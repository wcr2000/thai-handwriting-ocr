"""เว็บแอประบบเอื้อเฟื้อที่จอดรถ: อัปโหลด -> OCR -> คิวตรวจสอบ -> ค้นหา -> รับรถกลับ

รัน:  uvicorn ocrslip.web.main:app --reload
"""

from __future__ import annotations

import datetime as dt
import io
import math
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlencode
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..auth import COOKIE_NAME, SESSION_TTL, User, authenticate, make_token, read_token
from ..config import (
    COOKIE_SECURE, ENTRY_BUILDINGS, ENTRY_FLOORS, ENTRY_PASSWORD, OCR_MODEL, SECRET_KEY, USD_THB,
)
from ..db import (
    add_staff, build_filters, clean_person_name, connect, dashboard_stats, export_rows, get_image, get_slip,
    delete_slip, get_settings, insert_slip, list_edits, open_slip_by_plate, set_setting,
    claim_next, claim_one, known_people, list_staff, mark_returned, next_in_queue,
    query_slips, release_claims, review_counts,
    set_staff_active, update_slip,
)
from ..normalize import PROVINCE_CHOICES, norm_phone, norm_plate, parse_date
from ..review import REASON_LABELS, evaluate
from ..search import search as fuzzy_search
from .pipeline import ingest

HERE = Path(__file__).resolve().parent
app = FastAPI(title="ระบบเอื้อเฟื้อที่จอดรถ")

# ถ้าไม่ได้ตั้ง SECRET_KEY ให้สุ่มขึ้นมาใช้ในรอบนี้ — ปลอดภัย แต่รีสตาร์ตแล้วทุกคนต้องล็อกอินใหม่
_SECRET = SECRET_KEY or secrets.token_urlsafe(32)

# หน้าที่เข้าได้โดยไม่ต้องล็อกอิน
# "/in" = ฟอร์มที่ผู้มาจอดกรอกเอง ต้องเปิดให้คนนอกเข้าได้ ด่านของหน้านี้คือรหัสที่
# เจ้าหน้าที่พิมพ์ปิดท้าย ไม่ใช่การล็อกอิน (ตรวจรหัสฝั่ง server เท่านั้น ดู config.ENTRY_PASSWORD)
PUBLIC_PATHS = ("/login", "/static", "/health", "/favicon.ico", "/in")
# หน้าที่เฉพาะ admin เท่านั้น — จุดที่ย้อนกลับไม่ได้ หรือเป็นข้อมูลส่วนตัวทั้งก้อน
ADMIN_ONLY = ("/table", "/dashboard", "/export.xlsx", "/staff", "/settings")
# "/reject" ไม่อยู่ในนี้ — คนที่นั่งตรวจ (approver) คือคนที่เห็นรูปเบลอ/ใบผิดประเภท
# ถ้าตีกลับไม่ได้ เขาจะกดอนุมัติข้อมูลขยะแทน ซึ่งแก้ยากกว่า (ตีกลับย้อนได้ด้วย ?edit=1)
# "/return" เคยอยู่ในนี้ แต่คนที่ยืนอยู่จุด checkout คือ staff ไม่ใช่ admin ถ้าปล่อยรถไม่ได้
# เขาจะปล่อยรถโดยไม่บันทึกอะไรเลย ซึ่งแย่กว่าการให้สิทธิ์ (approver ยังถูกกันด้วย allowlist ล่าง)
# "/delete" ตรงข้าม — ย้อนกลับไม่ได้และลบรูปหลักฐานทิ้งด้วย จึงต้องเป็น admin เท่านั้น
ADMIN_ONLY_SUFFIX = ("/delete",)
# role "approver" (คนทำ label) ใช้ allowlist ไม่ใช่ blacklist — route ใหม่ที่ลืมคิดถึงสิทธิ์
# จะถูกปิดไว้ก่อนเสมอ ไม่ใช่เปิดให้โดยบังเอิญ เขาเห็นแค่ "อัปโหลด" กับ "คิวตรวจ" เท่านั้น
# (ค้นหา / ใบรายตัว / ตารางข้อมูล / สรุป / รายชื่อทีม ปิดหมด)
APPROVER_PATHS = ("/", "/api/ocr", "/logout", "/review")
APPROVER_PREFIXES = ("/review/", "/image/")
class NoCacheStatic(StaticFiles):
    """บังคับให้เบราว์เซอร์เช็กกับ server ก่อนใช้ไฟล์เก่าเสมอ

    ไฟล์ CSS เล็กมากและเปลี่ยนทุกครั้งที่ deploy ถ้าปล่อยให้ cache แบบเดาเอง
    หน้าเว็บจะเพี้ยนหลัง deploy จนกว่าผู้ใช้จะล้าง cache เอง
    no-cache ไม่ได้แปลว่าห้ามเก็บ — ยังใช้ ETag ตอบ 304 ได้ ไม่เปลืองเน็ต
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", NoCacheStatic(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["reason_labels"] = REASON_LABELS
templates.env.globals["usd_thb"] = USD_THB


def axis_ticks(top: int, divisions: int = 4) -> list[int]:
    """หาเส้นกริดแกน Y ที่เป็นเลขกลม ๆ และครอบค่าสูงสุดพอดี

    ถ้าปล่อยให้เพดานแกนเป็นค่าสูงสุดดิบ ป้ายกริดจะกลายเป็นเลขอย่าง 1,217
    ซึ่งอ่านแล้วเทียบแท่งอื่นไม่ได้ — แกนมีหน้าที่บอกค่าของแท่งที่ไม่ได้ติดป้ายไว้
    """
    if top <= 0:
        return [0, 1]
    step = top / divisions
    mag = 10 ** math.floor(math.log10(step))
    for m in (1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step <= m * mag:
            step = m * mag
            break
    step = max(1, int(round(step)))
    return [step * i for i in range(divisions + 1)]


templates.env.globals["axis_ticks"] = axis_ticks

# ชื่อช่องที่คนอ่านรู้เรื่อง — ใช้กับกราฟ "ช่องที่เจ้าหน้าที่ต้องแก้"
FIELD_LABELS = {
    "name": "ชื่อ-นามสกุล", "tel": "เบอร์โทร", "plate_raw": "ทะเบียน", "brand": "ยี่ห้อ",
    "car_type": "ประเภทรถ", "location": "ที่จอด", "deposit_date": "วันที่", "province": "จังหวัด",
}


def _relabel(row: dict, table: dict) -> dict:
    """แทนชื่อ key ดิบจาก DB ด้วยชื่อภาษาคน โดยไม่แก้ dict เดิม"""
    return {**row, "label": table.get(row["label"], row["label"])}


templates.env.filters["to_reason"] = lambda r: _relabel(r, REASON_LABELS)
templates.env.filters["to_field"] = lambda r: _relabel(r, FIELD_LABELS)

FORM_FIELDS = ("name", "tel", "date", "noplate", "province", "brand", "typecar", "location")


def current_user(request: Request) -> User | None:
    token = request.cookies.get(COOKIE_NAME, "")
    return read_token(token, _SECRET) if token else None


# คุกกี้ที่บอกว่า "เบราว์เซอร์นี้คือใคร" ใช้เป็นเจ้าของการจองใบในคิว
# ใช้ session token ไม่ได้เพราะสามคนล็อกอินบัญชี staff เดียวกัน payload จึงเหมือนกันหมด
# ไม่ต้องเซ็นเพราะปลอมไปก็ได้แค่แย่งใบที่ตัวเองก็เข้าถึงได้อยู่แล้ว ไม่ใช่ขอบเขตสิทธิ์
WORKER_COOKIE = "ocrslip_worker"
WORKER_TTL = 30 * 24 * 3600


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """ปิดทุกหน้าที่ไม่ได้อยู่ใน PUBLIC_PATHS และบังคับสิทธิ์ admin ในหน้าที่กำหนด"""
    path = request.url.path
    if path.startswith(PUBLIC_PATHS):
        return await call_next(request)

    user = current_user(request)
    if user is None:
        if request.headers.get("HX-Request"):  # คำขอจาก HTMX ให้สั่งเบราว์เซอร์เด้งไปหน้า login
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse(f"/login?next={quote(str(request.url.path))}", status_code=303)

    if not user.is_admin and (path.startswith(ADMIN_ONLY) or path.endswith(ADMIN_ONLY_SUFFIX)):
        return _forbidden("หน้านี้สำหรับผู้ดูแลระบบเท่านั้น")

    if user.is_approver and not (path in APPROVER_PATHS or path.startswith(APPROVER_PREFIXES)):
        return _forbidden("บัญชีนี้ใช้ได้เฉพาะหน้าอัปโหลดกับคิวตรวจสอบ")

    request.state.user = user
    worker = request.cookies.get(WORKER_COOKIE, "")
    fresh = not (worker.isalnum() and len(worker) == 32)
    if fresh:
        worker = secrets.token_hex(16)
    request.state.worker = worker
    resp = await call_next(request)
    if fresh:
        resp.set_cookie(WORKER_COOKIE, worker, max_age=WORKER_TTL,
                        httponly=True, samesite="lax", secure=COOKIE_SECURE)
    return resp


@app.middleware("http")
async def no_stale_html(request: Request, call_next):
    """ห้ามเบราว์เซอร์ cache หน้า HTML

    ต้องประกาศ "หลัง" auth_gate เพราะ Starlette วาง middleware ที่ลงทะเบียนทีหลังไว้ชั้นนอกสุด
    ถ้าอยู่ก่อน response ที่ auth_gate คืนเอง (303 เด้งไป login, 403 หน้าไม่มีสิทธิ์)
    จะไม่ผ่าน middleware นี้เลย จึงไม่มี Cache-Control ติดไป

    ก่อนหน้านี้ไม่ได้ส่ง Cache-Control มาเลย เบราว์เซอร์ (โดยเฉพาะ Safari บน iOS)
    จึงใช้ heuristic caching เดาเอาเองว่าเก็บได้ ผลคือหลัง deploy ผู้ใช้ยังเห็นฟอร์มเวอร์ชันเก่า
    ขณะที่ server เป็นเวอร์ชันใหม่ — ฟอร์มเก่าไม่มีช่องที่ server ใหม่บังคับ กดแล้วพังทันที
    ข้อมูลในหน้าเว็บนี้เปลี่ยนตลอด (คิวตรวจ ผลค้นหา) และเป็นข้อมูลส่วนบุคคล
    จึงไม่ควรถูกเก็บไว้ในเครื่องอยู่แล้ว
    """
    response = await call_next(request)

    # ห้ามดูจาก content-type เพราะ redirect 303 ไม่มี body จึงไม่มี content-type
    # ถ้าเช็ค "text/html" หน้าที่เด้งไป login จะหลุดไม่ได้ header
    # ข้าม /static (มี no-cache ของตัวเอง) กับ /image (รูปหลักฐานไม่เปลี่ยน cache ได้นาน)
    # และไม่ทับค่าที่ route ตั้งไว้เองแล้ว
    if (
        not request.url.path.startswith(("/static", "/image"))
        and "cache-control" not in response.headers
    ):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


def _forbidden(message: str) -> HTMLResponse:
    return HTMLResponse(
        f"<h3 style='font-family:sans-serif;padding:2rem'>{message}"
        " · <a href='/'>กลับหน้าแรก</a></h3>", status_code=403)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", getattr(request.state, "user", None))
    return templates.TemplateResponse(request, name, ctx)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": error})


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                 or (request.client.host if request.client else "unknown"))
    user, error = authenticate(str(form.get("username", "")), str(form.get("password", "")), client_ip)
    nxt = str(form.get("next") or "/")
    if not nxt.startswith("/"):  # กัน open redirect
        nxt = "/"
    if user is None:
        return templates.TemplateResponse(
            request, "login.html", {"next": nxt, "error": error, "username": form.get("username", "")},
            status_code=401,
        )
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(
        COOKIE_NAME, make_token(user, _SECRET), max_age=SESSION_TTL,
        httponly=True, samesite="lax", secure=COOKIE_SECURE,
    )
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---------- ฟอร์มขาเข้าที่ผู้มาจอดกรอกเอง ----------
# ทางนี้ไม่มี OCR และไม่เข้าคิวตรวจ เพราะคนที่รู้ข้อมูลคือคนที่พิมพ์ข้อมูลเอง
# ไม่มีอะไรให้ "อ่านออกไหม" อีกแล้ว คิวตรวจจึงเหลือรับแค่ใบเขียนมือที่ถ่ายรูปเข้ามา

CAR_TYPES = ("เก๋ง", "กระบะ", "ตู้")

# จังหวัดที่ยกขึ้นไว้กลุ่มแรกของ dropdown — วัดอยู่ปทุมธานี รถส่วนใหญ่มาจากแถวนี้
# บน iOS การเลือกจังหวัดคือการหมุนวงล้อ ถ้าไม่ยกขึ้นมาก็ต้องหมุนผ่านหลายสิบช่องทุกคัน
COMMON_PROVINCES = ("ปทุมธานี", "กรุงเทพมหานคร", "นนทบุรี", "นครปฐม", "สมุทรปราการ",
                    "พระนครศรีอยุธยา", "นครนายก", "สระบุรี")


# คีย์ใน app_settings — ต้องมีที่เดียว ไม่งั้นหน้าอ่านกับหน้าเขียนสะกดต่างกันเมื่อไหร่
# ค่าที่ตั้งไว้จะหายเงียบ ๆ โดยไม่มีอะไรพัง
SETTING_BUILDINGS = "entry_buildings"
SETTING_FLOORS = "entry_floors"


def _lines(text: str) -> tuple[str, ...]:
    """แปลง textarea เป็นรายการตัวเลือก ตัดบรรทัดว่างและช่องว่างหัวท้ายทิ้ง"""
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _entry_lists(conn) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """อาคาร/ชั้นที่ใช้จริง — ตั้งจากหน้าเว็บได้ ถ้ายังไม่เคยตั้งก็ใช้ค่าใน env

    ต้องอ่านทุก request ไม่ใช่ cache ไว้ตอน process เริ่ม เพราะเหตุผลทั้งหมดที่ย้าย
    มาไว้บนหน้าเว็บคือ "แก้แล้วมีผลทันทีโดยไม่ต้องรีสตาร์ต" ตารางนี้มีไม่กี่แถว
    """
    saved = get_settings(conn)
    return (_lines(saved.get(SETTING_BUILDINGS, "")) or ENTRY_BUILDINGS,
            _lines(saved.get(SETTING_FLOORS, "")) or ENTRY_FLOORS)


def _entry_choices(buildings, floors) -> dict[str, Any]:
    return {"provinces": PROVINCE_CHOICES, "common_provinces": COMMON_PROVINCES,
            "buildings": buildings, "floors": floors, "car_types": CAR_TYPES}


@app.get("/in", response_class=HTMLResponse)
def entry_form(request: Request):
    if not ENTRY_PASSWORD:
        return HTMLResponse("ยังไม่ได้เปิดใช้ฟอร์มนี้ (ผู้ดูแลระบบยังไม่ได้ตั้ง ENTRY_PASSWORD)",
                            status_code=503)
    today = dt.date.today().isoformat()
    with connect() as conn:
        buildings, floors = _entry_lists(conn)
    return render(request, "in.html", errors={}, v={"date": today},
                  **_entry_choices(buildings, floors))


@app.post("/in", response_class=HTMLResponse)
async def entry_submit(request: Request):
    if not ENTRY_PASSWORD:
        return HTMLResponse("ยังไม่ได้เปิดใช้ฟอร์มนี้", status_code=503)

    form = await request.form()
    with connect() as conn:
        buildings, floors = _entry_lists(conn)
    v = {k: (form.get(k) or "").strip() for k in
         ("name", "tel", "date", "noplate", "province", "brand", "typecar", "typecar_new",
          "building", "floor")}
    car_type = v["typecar_new"] if v["typecar"] == "__new__" else v["typecar"]

    errors: dict[str, str] = {}
    if len(clean_person_name(v["name"])) < 4 or " " not in clean_person_name(v["name"]):
        errors["name"] = "กรอกทั้งชื่อและนามสกุล"
    if len(norm_phone(v["tel"])) != 10:
        errors["tel"] = "เบอร์โทรต้องเป็นตัวเลข 10 หลัก"
    if not norm_plate(v["noplate"]):
        errors["noplate"] = "กรอกทะเบียนรถ"
    if v["province"] not in PROVINCE_CHOICES:
        errors["province"] = "เลือกจังหวัดของทะเบียน"
    if v["building"] not in buildings:
        errors["building"] = "เลือกอาคารที่จอด"
    if v["floor"] not in floors:
        errors["floor"] = "เลือกชั้นที่จอด"
    if not car_type:
        errors["typecar"] = "เลือกชนิดรถ"
    if parse_date(v["date"]) is None:
        errors["date"] = "วันที่ไม่ถูกต้อง"
    # เทียบด้วย compare_digest ไม่ใช่ == เพื่อไม่ให้เวลาที่ใช้เทียบบอกว่าถูกกี่ตัวแรก
    # ต้องเทียบเป็น bytes: compare_digest ปฏิเสธ str ที่มีอักขระนอก ASCII ซึ่งรหัสภาษาไทยเข้าข่าย
    # ไม่มี rate limit ที่นี่โดยเจตนา (ตกลงกันไว้ว่าไม่เพิ่มระบบ) — ด่านจริงคือ
    # เจ้าหน้าที่เป็นคนขอเครื่องมาพิมพ์รหัสเอง ไม่ได้บอกรหัสให้ผู้มาจอด
    if not secrets.compare_digest(
        (form.get("entry_pw") or "").encode(), ENTRY_PASSWORD.encode()
    ):
        errors["entry_pw"] = "รหัสเจ้าหน้าที่ไม่ถูกต้อง"

    if errors:
        return render(request, "in.html", errors=errors, v=v,
                      **_entry_choices(buildings, floors))

    fields = {"name": clean_person_name(v["name"]), "tel": v["tel"], "date": v["date"],
              "noplate": v["noplate"], "province": v["province"], "brand": v["brand"] or None,
              "typecar": car_type, "location": f"{v['building']} {v['floor']}"}

    with connect() as conn:
        # กันกดส่งซ้ำ (refresh หน้า / กดปุ่มสองที) ไม่ให้กลายเป็นสองใบของรถคันเดียว
        # ถ้ามีใบของทะเบียนนี้ที่ยังไม่ได้รับรถกลับและเพิ่งลงทะเบียนไปวันนี้ ให้คืนใบเดิม
        # ใบซ้ำเจ็บที่ขาออก: เจ้าหน้าที่จะเห็นสองแถวเหมือนกันแล้วไม่รู้ว่าต้องปิดใบไหน
        dup = open_slip_by_plate(conn, norm_plate(v["noplate"]))
        if dup:
            slip = dup
        else:
            # review_reason ว่าง -> insert_slip ตั้ง needs_review = false ให้เอง
            slip_id = insert_slip(
                conn, fields,
                confidence={}, raw_ocr={}, review_reason=[],
                ocr_model=None, ocr_variant=None,
                # กรอกเองแล้วเจ้าหน้าที่ยืนยันแล้ว = ไม่มีอะไรให้ตรวจ เข้าสถานะพร้อมค้นหาเลย
                review_status="approved", entry_source="typed",
            )
            conn.commit()
            slip = get_slip(conn, slip_id)

    return render(request, "in_done.html", slip=slip, again=bool(dup))


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with connect() as conn:
        return render(request, "index.html", counts=review_counts(conn), model=OCR_MODEL,
                      people=known_people(conn))


@app.post("/api/ocr", response_class=HTMLResponse)
async def api_ocr(
    request: Request,
    files: list[UploadFile],
    uploaded_by: str = Form(""),
    photographer: str = Form(""),
    uploaded_by_new: str = Form(""),
    photographer_new: str = Form(""),
):
    """อัปโหลดได้หลายใบพร้อมกัน — ทุกใบเข้าคิว pending รอคนตรวจเสมอ

    ประมวลผลขนานกัน เพราะเวลาเกือบทั้งหมดหมดไปกับการรอ LLM ตอบ
    (คีย์ย้อนหลังเป็นร้อยใบแบบทีละใบจะช้าเกินใช้งาน)
    """
    # created_by = บัญชีที่ล็อกอิน (ปลอมไม่ได้), uploaded_by/photographer = ชื่อคนจริงที่เลือกมา
    account = getattr(request.state, "user", None) and request.state.user.username
    # ค่า "__new__" คือผู้ใช้เลือก "+ ชื่อใหม่" ในรายการ แล้วไปพิมพ์ในช่องข้าง ๆ
    def pick(choice: str, typed: str) -> str | None:
        raw = typed if choice.strip() == "__new__" else choice
        # clean_person_name ตัดอักขระล่องหนและปฏิเสธค่า sentinel
        # ไม่งั้นจะได้ชื่อที่ว่างเปล่าในสายตาคนแต่ระบบนับว่ามีค่า
        return clean_person_name(raw) or None

    uploader = pick(uploaded_by, uploaded_by_new)
    shooter = pick(photographer, photographer_new) or uploader

    # ต้องตรวจฝั่ง server ด้วย เพราะ required ใน HTML ข้ามได้ถ้ายิง API ตรง ๆ
    # ถ้าปล่อยผ่าน จะได้ใบที่ไม่รู้ว่าใครเป็นคนบันทึก ซึ่งเป็นสิ่งที่ feature นี้มีไว้กันพอดี
    if not uploader:
        return render(request, "partials/upload_result.html", results=[],
                      uploader=None, shooter=None,
                      error="ต้องระบุชื่อคนอัปโหลดก่อน จะได้รู้ว่าใบนี้ใครเป็นคนบันทึก")
    # ชื่อที่ยังไม่อยู่ในรายการ ให้เพิ่มเข้าไปเลย ไม่บล็อกคนหน้างานตอนฉุกเฉิน
    # admin ไปปิดหรือจัดระเบียบทีหลังได้ที่หน้า /staff
    with connect() as conn:
        for person in {uploader, shooter}:
            if person:
                add_staff(conn, person, created_by=account)
        conn.commit()

    uploads = [(f.filename, await f.read()) for f in files]
    uploads = [(name, raw) for name, raw in uploads if raw]

    def one(item: tuple[str, bytes]) -> dict[str, Any]:
        name, raw = item
        try:
            with connect() as conn:  # หนึ่ง connection ต่อหนึ่ง thread
                return {**ingest(conn, raw, created_by=account,
                                 uploaded_by=uploader, photographer=shooter), "filename": name}
        except Exception as exc:  # ใบเดียวพังต้องไม่ทำให้ทั้ง batch ล่ม
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "filename": name}

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(uploads)))) as pool:
        results = list(pool.map(one, uploads))
    return render(request, "partials/upload_result.html", results=results,
                  uploader=uploader, shooter=shooter)


# กองที่ approver เปิดดูได้ — เฉพาะงานที่ยังค้างอยู่ ไม่ใช่คลังใบทั้งหมดที่ผ่านมา
APPROVER_FILTERS = ("needs", "quick")

# แต่ละ "กอง" คือ preset ของตัวกรองชุดเดียวกับหน้าตาราง ไม่ใช่ query คนละแบบ
QUEUE_PILES = {
    "needs": dict(review_status="pending", needs_review=True),
    "quick": dict(review_status="pending", needs_review=False),
    "approved": dict(review_status="approved"),
    "rejected": dict(review_status="rejected"),
    "all": {},
}

# ยอดของแต่ละกองตรงกับคอลัมน์ไหนใน review_counts() — ใช้แทนการนับใหม่ตอนไม่ได้ค้นหา
PILE_TOTALS = {"needs": "needs_review", "quick": "quick_pass",
               "approved": "approved", "rejected": "rejected", "all": "total"}


@app.get("/review", response_class=HTMLResponse)
def review_queue(
    request: Request, filter: str = "needs",
    q: str = "", sort: str = "created_at", dir: str = "desc", page: int = 1,
):
    """คิวตรวจ — ใช้ตัวกรอง/ค้นหา/เรียง/แบ่งหน้าชุดเดียวกับหน้าตาราง

    กองที่เลือกถูกบังคับทับคำค้นเสมอ เพื่อให้ approver ค้นได้แต่ไม่หลุดออกนอกกองของตัวเอง
    """
    user = getattr(request.state, "user", None)
    if user and user.is_approver and filter not in APPROVER_FILTERS:
        filter = "needs"
    pile = QUEUE_PILES.get(filter, {})
    filters = build_filters(q=q, **pile)
    per_page = 50
    with connect() as conn:
        # ถ้าไม่ได้ค้นหา ยอดรวมของกองมีอยู่ใน review_counts() ที่ยังไงก็ต้องยิงเพื่อทำแถบกองอยู่แล้ว
        # จึงไม่ต้องให้ query_slips ไปนับซ้ำ — ที่ 33,000 ใบต่างกัน 0.2 ms กับ 96 ms
        counts = review_counts(conn)
        known_total = PILE_TOTALS.get(filter) if not (q and q.strip()) else None
        slips, total = query_slips(conn, filters=filters, sort=sort,
                                   desc=(dir != "asc"), page=max(1, page), per_page=per_page,
                                   total=counts[known_total] if known_total else None)
    qs = urlencode({k: v for k, v in
                    {"filter": filter, "q": q, "sort": sort, "dir": dir}.items() if v})
    return render(
        request, "review_list.html", slips=slips, counts=counts, active=filter,
        # กองที่ยังไม่ได้ตรวจยังไม่มีใครเป็นเจ้าของ คอลัมน์ "คนตรวจ" จะว่างทั้งแถว
        show_reviewer=(filter in ("approved", "rejected", "all")),
        total=total, page=max(1, page), per_page=per_page,
        pages=max(1, -(-total // per_page)), qs=qs,
        f={"q": q, "sort": sort, "dir": dir},
    )


@app.get("/review/next")
def review_next(request: Request):
    """จองใบถัดไปในคิว ณ ตอนนี้ แล้วพาไปที่ใบนั้น

    เดิมปุ่ม "ใบถัดไป" พาไปตาม id ที่คำนวณไว้ตั้งแต่ตอน render ซึ่งเป็นภาพคิวเมื่อกี้
    ไม่ใช่ตอนนี้ คนที่ตรวจช้ากว่าจึงเดินตาม id เก่าไปโผล่ใบที่เพื่อนเพิ่งตรวจเสร็จ
    เปลี่ยนมาถามฐานข้อมูลสด ๆ ตอนกด และจองไว้ในคำสั่งเดียวกัน คนอื่นจะไม่ได้ใบนี้อีก
    """
    with connect() as conn:
        nxt = claim_next(conn, request.state.worker)
        conn.commit()
    return RedirectResponse(f"/review/{nxt}" if nxt else "/review", status_code=303)


@app.get("/review/{slip_id}", response_class=HTMLResponse)
def review_one(request: Request, slip_id: str, edit: int = 0):
    """หน้าตรวจ 1 ใบ — edit=1 คือยืนยันว่าจะแก้ใบที่ตรวจไปแล้วจริง ๆ

    คิวยื่นใบหัวแถวใบเดียวกันให้ทุกคน (next_in_queue) และปุ่ม "ใบถัดไป" พาไปตาม
    id ที่คำนวณไว้ตอนเปิดหน้า คนที่ตรวจช้ากว่าจึงมาโผล่ใบที่เพื่อนเพิ่งตรวจเสร็จได้เสมอ
    ถ้าปล่อยให้ฟอร์มขึ้นตามปกติ ช่อง "ผู้ตรวจ" จะถูก preselect เป็นชื่อคนที่ตรวจไปแล้ว
    (เทมเพลตให้ค่าใน DB ชนะค่าที่จำไว้ในเครื่อง) แล้วถ้ากดอนุมัติต่อก็ทับงานเพื่อนทันที
    """
    with connect() as conn:
        slip = get_slip(conn, slip_id)
        if not slip:
            return HTMLResponse("ไม่พบใบนี้", status_code=404)
        # เปิดใบไหนก็จองใบนั้น เผื่อคนคลิกจากรายการคิวแทนที่จะกดปุ่มใบถัดไป
        # ถ้าจองไม่ได้แปลว่ามีคนอื่นถืออยู่ — เตือนเฉย ๆ ไม่บล็อก เพราะอาจเป็น
        # คนเดียวกันเปิดจากอีกเครื่อง และด่านจริงคือ require_status ตอนกดอนุมัติอยู่แล้ว
        held = claim_one(conn, slip_id, request.state.worker)
        conn.commit()
        # ใบถัดไปในคิว: เอาที่ต้องตรวจก่อน ถ้าหมดค่อยไล่ใบที่รอยืนยันเฉย ๆ
        next_id, remaining = next_in_queue(conn, slip_id)
        return render(
            request, "review_detail.html",
            slip=slip, problems=(slip.get("raw_ocr") or {}).get("problems", {}),
            next_id=next_id, remaining=remaining,
            people=known_people(conn),
            taken=(slip["review_status"] != "pending" and not edit),
            held_by=held,
        )


def _form_fields(form) -> dict[str, Any]:
    return {f: (form.get(f) or "").strip() or None for f in FORM_FIELDS}


def _picked_reviewer(form) -> str | None:
    """ชื่อคนจริงที่เลือกในช่อง "ผู้ตรวจ" ("__new__" = ขอพิมพ์ชื่อใหม่)

    บัญชี staff/approve ใช้กันหลายคน ถ้าบันทึกชื่อบัญชีไว้จะตามไม่ได้ว่าใครเป็นคนตรวจใบไหน
    """
    return clean_person_name(
        form.get("reviewed_by_new") if (form.get("reviewed_by") or "").strip() == "__new__"
        else form.get("reviewed_by")
    ) or None


@app.post("/review/{slip_id}/approve")
async def approve(request: Request, slip_id: str):
    form = await request.form()
    fields = _form_fields(form)
    account = getattr(request.state, "user", None) and request.state.user.username
    # บังคับให้เลือกชื่อคนจริงแบบเดียวกับตอนอัปโหลด จะได้รู้ว่าใบนี้ใครอนุมัติ
    reviewer = _picked_reviewer(form)
    force = (form.get("force") or "") == "1"
    # ตรวจซ้ำหลังคนแก้ แต่บล็อกเฉพาะ "ช่องบังคับที่ยังว่าง" เท่านั้น
    # ส่วนรูปแบบแปลก ๆ (ทะเบียนไม่มีหมวดอักษร, วันที่เขียนแค่ '26') เป็นแค่คำเตือน
    # เพราะคนตรวจเห็นรูปใบจริงแล้ว และของจริงก็มีใบแบบนั้นอยู่จริง
    reasons, problems = evaluate(fields, {})
    blocking = {f: msg for f, msg in problems.items() if not fields.get(f)}
    with connect() as conn:
        # ต้องเช็กฝั่ง server ด้วย เพราะ required ใน HTML ข้ามได้ถ้ายิง API ตรง ๆ
        if blocking or not reviewer:
            slip = get_slip(conn, slip_id)
            if not slip:  # ใบถูกลบ/ตีกลับไปแล้วระหว่างคนตรวจเปิดค้างไว้
                return HTMLResponse("ไม่พบใบนี้", status_code=404)
            return render(
                request, "review_detail.html",
                slip={**slip, **{k: v for k, v in fields.items() if v}},
                problems=blocking, next_id=None, remaining=0, people=known_people(conn),
                error=("ยังมีช่องบังคับที่ว่างอยู่ (ชื่อ / เบอร์โทร / ทะเบียน) กรอกให้ครบก่อนอนุมัติ"
                       if blocking else
                       "ต้องระบุชื่อผู้ตรวจก่อน จะได้รู้ว่าใบนี้ใครเป็นคนอนุมัติ"),
            )
        # ชื่อที่ยังไม่อยู่ในรายการ ให้เพิ่มเข้าไปเลย ไม่บล็อกคนตรวจตอนงานเข้าพร้อมกันเยอะ ๆ
        add_staff(conn, reviewer, created_by=account)
        # require_status กันสองคนที่เปิดใบเดียวกันค้างไว้ เขียนทับกันโดยไม่มีใครรู้
        # ใบที่เจ้าของตั้งใจกลับมาแก้เองจะมาทาง ?edit=1 ซึ่งข้ามด่านนี้ได้
        written = update_slip(conn, slip_id, fields, edited_by=reviewer,
                              review_status="approved", review_reason=[],
                              require_status=None if force else "pending")
        if written is None:
            conn.rollback()
            slip = get_slip(conn, slip_id)
            if not slip:
                return HTMLResponse("ไม่พบใบนี้", status_code=404)
            next_id, remaining = next_in_queue(conn, slip_id)
            return render(
                request, "review_detail.html",
                slip=slip, problems={}, next_id=next_id, remaining=remaining,
                people=known_people(conn), taken=True,
            )
        conn.commit()
    # ไม่ใช้ next_id ที่ฝังมากับฟอร์มแล้ว — มันเป็นภาพคิวตอนเปิดหน้า ซึ่งอาจผ่านไปหลายนาที
    # /review/next จองใบสด ๆ ให้ตอนนี้ คนอื่นจะไม่ได้ใบเดียวกัน
    return RedirectResponse("/review/next", status_code=303)


@app.post("/review/{slip_id}/reject")
async def reject(request: Request, slip_id: str):
    form = await request.form()
    # ชื่อผู้ตรวจต้องผ่านตัวกรองเดียวกับตอนอนุมัติ ไม่งั้นค่า sentinel "__new__"
    # จะถูกบันทึกเป็นชื่อคน แล้วโผล่ในสถิติว่ามีคนชื่อ __new__ ตีกลับไปหลายใบ
    reviewer = _picked_reviewer(form)
    with connect() as conn:
        conn.execute(
            "UPDATE ocr_dhammakaya.slips SET review_status='rejected', needs_review=false,"
            " review_reason=%s, reviewed_by=%s, reviewed_at=now(),"
            # ใบที่ออกจากกอง pending แล้วไม่ต้องมีใครถืออีก
            " claimed_by=NULL, claimed_name=NULL, claimed_at=NULL WHERE id=%s",
            ([(form.get("reason") or "รูปอ่านไม่ได้")], reviewer, slip_id),
        )
        conn.commit()
    return RedirectResponse("/review", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, deleted: str = ""):
    return render(request, "search.html", deleted=deleted)


@app.get("/api/search", response_class=HTMLResponse)
def api_search(request: Request, q: str = "", include_pending: bool = False):
    with connect() as conn:
        rows = fuzzy_search(conn, q, include_pending=include_pending) if q else []
    return render(request, "partials/results.html", rows=rows, q=q)


@app.get("/slips/{slip_id}", response_class=HTMLResponse)
def slip_detail(request: Request, slip_id: str, taken: int = 0):
    with connect() as conn:
        slip = get_slip(conn, slip_id)
        if not slip:
            return HTMLResponse("ไม่พบใบนี้", status_code=404)
        # เคยเขียนชื่อ schema ตายตัวว่า ocr_dhammakaya ตรงนี้ ทำให้หน้าใบพัง 500
        # ทุกครั้งที่ DB_SCHEMA ถูกตั้งเป็นอย่างอื่น (เช่นตอนรันเทสต์) — ย้ายไปใช้ของ db.py
        edits = list_edits(conn, slip_id)
        # ใบที่กรอกเองไม่มีรูปหลักฐาน ต้องรู้ก่อน render ไม่งั้นหน้าจะมีกรอบรูปแตกค้างอยู่
        has_image = get_image(conn, slip_id) is not None
    return render(request, "slip.html", slip=slip, edits=edits, has_image=has_image,
                  taken=bool(taken))


@app.post("/slips/{slip_id}/return")
async def do_return(request: Request, slip_id: str):
    form = await request.form()
    with connect() as conn:
        ok = mark_returned(
            conn, slip_id,
            (form.get("returned_by") or None),
            (form.get("note") or None),
            clean_person_name(form.get("released_to") or "") or None,
        )
        conn.commit()
    # ปิดไม่สำเร็จ = มีคนปิดไปก่อนแล้ว ต้องบอกให้เห็นชัด ไม่ใช่เด้งกลับหน้าเดิมเงียบ ๆ
    # เหมือนกดสำเร็จ — เจ้าหน้าที่จะไม่รู้ว่ารถคันนี้อาจถูกปล่อยให้คนอื่นไปแล้ว
    return RedirectResponse(f"/slips/{slip_id}" + ("" if ok else "?taken=1"), status_code=303)


@app.post("/slips/{slip_id}/delete")
async def do_delete(request: Request, slip_id: str):
    """ลบใบถาวร — สำหรับใบทดสอบ/ใบกรอกมั่ว ที่เก็บไว้มีแต่ทำให้ตัวเลขสรุปเพี้ยน"""
    account = getattr(request.state, "user", None) and request.state.user.username
    with connect() as conn:
        gone = delete_slip(conn, slip_id)
        conn.commit()
    if not gone:
        return HTMLResponse("ไม่พบใบนี้ (อาจถูกลบไปแล้ว)", status_code=404)
    # ไม่มีตาราง audit ของการลบ เพราะ slip_edits ถูก cascade ทิ้งไปพร้อมใบอยู่แล้ว
    # จึงบันทึกลง log ของ process แทน — Render เก็บ log ไว้ให้ย้อนดูได้ว่าใครลบใบไหนเมื่อไหร่
    print(f"[delete] slip={gone['id']} plate={gone.get('plate_raw')!r} "
          f"name={gone.get('name')!r} car_status={gone.get('car_status')} by={account}")
    return RedirectResponse(
        f"/search?deleted={quote(str(gone.get('plate_raw') or gone['id']))}", status_code=303)

@app.get("/image/{slip_id}")
def image(slip_id: str, kind: str = "processed"):
    with connect() as conn:
        data = get_image(conn, slip_id, kind)
    if not data:
        return Response(status_code=404)
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    with connect() as conn:
        stored = get_settings(conn)
        buildings, floors = _entry_lists(conn)
    return render(
        request, "settings.html",
        buildings="\n".join(buildings), floors="\n".join(floors),
        # บอกให้ชัดว่าค่าที่เห็นมาจากไหน ไม่งั้นผู้ดูแลจะไม่รู้ว่ากำลังดูค่าตั้งต้นจาก env
        # อยู่หรือดูค่าที่ตัวเองตั้งไว้ แล้วลบทิ้งโดยคิดว่า "ลบแล้วก็ยังเป็นค่านี้แหละ"
        from_db={"buildings": SETTING_BUILDINGS in stored, "floors": SETTING_FLOORS in stored},
        entry_open=bool(ENTRY_PASSWORD), saved=bool(saved))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    account = getattr(request.state, "user", None) and request.state.user.username
    with connect() as conn:
        set_setting(conn, SETTING_BUILDINGS, str(form.get("buildings") or ""), account)
        set_setting(conn, SETTING_FLOORS, str(form.get("floors") or ""), account)
        conn.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)

@app.get("/staff", response_class=HTMLResponse)
def staff_page(request: Request, error: str = ""):
    with connect() as conn:
        return render(request, "staff.html", staff=list_staff(conn), error=error)


@app.post("/staff")
async def staff_add(request: Request):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    account = getattr(request.state, "user", None) and request.state.user.username
    error = ""
    if not name:
        error = "กรุณากรอกชื่อ"
    else:
        with connect() as conn:
            if not add_staff(conn, name, created_by=account):
                error = f"มีชื่อ “{name}” อยู่ในรายการแล้ว"
            conn.commit()
    return RedirectResponse(f"/staff?error={quote(error)}" if error else "/staff", status_code=303)


@app.post("/staff/{staff_id}/toggle")
async def staff_toggle(request: Request, staff_id: int):
    form = await request.form()
    with connect() as conn:
        set_staff_active(conn, staff_id, str(form.get("active", "")) == "true")
        conn.commit()
    return RedirectResponse("/staff", status_code=303)


@app.get("/table", response_class=HTMLResponse)
def table_view(
    request: Request,
    q: str = "", review_status: str = "", car_status: str = "", car_type: str = "",
    uploaded_by: str = "", reviewed_by: str = "",
    sort: str = "created_at", dir: str = "desc", page: int = 1,
):
    """ตารางข้อมูลทั้งหมด พร้อมตัวกรอง/เรียง/แบ่งหน้า — ไม่โหลดรูปเพื่อให้หน้าเบา"""
    filters = build_filters(q=q, review_status=review_status,
                            car_status=car_status, car_type=car_type,
                            uploaded_by=uploaded_by, reviewed_by=reviewed_by)
    per_page = 50
    with connect() as conn:
        rows, total = query_slips(conn, filters=filters, sort=sort,
                                  desc=(dir != "asc"), page=max(1, page), per_page=per_page)
    qs = urlencode({k: v for k, v in
                    {"q": q, "review_status": review_status, "car_status": car_status,
                     "car_type": car_type, "uploaded_by": uploaded_by,
                     "reviewed_by": reviewed_by, "sort": sort, "dir": dir}.items() if v})
    return render(
        request, "table.html", rows=rows, total=total, page=max(1, page),
        per_page=per_page, pages=max(1, -(-total // per_page)), qs=qs,
        f={"q": q, "review_status": review_status, "car_status": car_status,
           "car_type": car_type, "uploaded_by": uploaded_by, "reviewed_by": reviewed_by,
           "sort": sort, "dir": dir},
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    with connect() as conn:
        return render(request, "dashboard.html", **dashboard_stats(conn))


@app.get("/export.xlsx")
def export_xlsx(
    q: str = "", review_status: str = "", car_status: str = "", car_type: str = "",
    uploaded_by: str = "", reviewed_by: str = "",
):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "ใบจอดรถ"
    headers = ["ชื่อ", "เบอร์โทร", "ทะเบียน", "จังหวัด", "ยี่ห้อ", "ประเภท", "ที่จอด",
               "วันที่เข้าจอด", "สถานะตรวจ", "สถานะรถ", "รับรถกลับเมื่อ", "ผู้ส่งมอบรถกลับ",
               "คนอัปโหลด", "คนถ่ายรูป", "คนตรวจ", "model", "บันทึกเมื่อ"]
    ws.append(headers)
    filters = build_filters(q=q, review_status=review_status,
                            car_status=car_status, car_type=car_type,
                            uploaded_by=uploaded_by, reviewed_by=reviewed_by)
    with connect() as conn:
        for r in export_rows(conn, filters):
            ws.append([
                r["name"], r["tel"], r["plate_raw"], r["province"], r["brand"], r["car_type"],
                r["location"], r["deposit_date"], r["review_status"], r["car_status"],
                r["returned_at"].replace(tzinfo=None) if r["returned_at"] else None,
                r["returned_by"],
                r["uploaded_by"], r["photographer"], r["reviewed_by"], r["ocr_model"],
                r["created_at"].replace(tzinfo=None) if r["created_at"] else None,
            ])
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = max(12, len(h) + 4)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="slips.xlsx"'},
    )
