"""The courtesy-parking web app: upload -> OCR -> review queue -> search -> car collected.

Run with:  uvicorn ocrslip.web.main:app --reload
"""

from __future__ import annotations

import datetime as dt
import hashlib
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
from ..daystamp import QUOTES, day_stamp, quote_cycle_days
from ..db import (
    add_car_check, add_staff, build_filters, clean_person_name, connect, dashboard_stats, export_rows, get_image, get_slip,
    delete_slip, deposit_history, deposit_rounds, finish_car_checks, get_settings, insert_slip,
    list_car_checks, list_edits, open_car_check, open_slip_by_plate, set_setting, slips_by_plate,
    claim_next, claim_one, known_people, list_staff, mark_returned, mark_superseded, next_in_queue,
    query_slips, reject_slip, release_claims, review_counts,
    set_staff_active, update_slip,
)
from ..normalize import (PROVINCE_CHOICES, norm_phone, norm_plate, normalize_field,
                         parse_date)
from ..review import REASON_LABELS, evaluate
from ..dedup import group_duplicates, load_slips
from ..search import search as fuzzy_search
from .pipeline import ingest

HERE = Path(__file__).resolve().parent
app = FastAPI(title="ระบบเอื้อเฟื้อที่จอดรถ")

# With no SECRET_KEY set, generate one for this process — safe, but a restart logs everyone out
_SECRET = SECRET_KEY or secrets.token_urlsafe(32)

# Pages reachable without logging in.
# "/in" = the self-service entry form, "/out" = the collection request form. All three have to be
# open to the public. What gates them is the code a staff member types to close the form, not
# a login. (Checked server-side only; see config.ENTRY_PASSWORD.)
# "/check" = a visit to a car that stays parked (start it, check it, take something out).
PUBLIC_PATHS = ("/login", "/static", "/health", "/favicon.ico", "/in", "/out", "/check")
# Admin-only pages — the irreversible actions, and the bulk personal data
ADMIN_ONLY = ("/table", "/dashboard", "/export.xlsx", "/staff", "/settings", "/dups")
# "/reject" is deliberately absent: the person at the review screen (the approver) is the one
# looking at the blurred photo or the wrong kind of document. Unable to reject, they would
# approve junk data instead, which is harder to undo (a rejection is reversible via ?edit=1).
# "/return" used to be here, but the person standing at checkout is staff, not an admin.
# Unable to release a car, they would release it and record nothing at all, which is worse
# than granting the permission. (Approvers are still kept out by the allowlist below.)
# "/delete" is the opposite case — irreversible, and it destroys the evidence images too, so
# it stays admin-only.
ADMIN_ONLY_SUFFIX = ("/delete",)
# The "approver" role (the labelling volunteers) is governed by an allowlist, not a blacklist:
# a new route whose permissions nobody thought about is closed by default rather than opened
# by accident. They see only upload and the review queue. (Search, individual slips, the data
# table, the dashboard and the team list are all closed to them.)
APPROVER_PATHS = ("/", "/api/ocr", "/logout", "/review")
APPROVER_PREFIXES = ("/review/", "/image/")
class NoCacheStatic(StaticFiles):
    """Force the browser to revalidate with the server before reusing a cached file.

    The CSS files are tiny and change on every deploy. Left to heuristic caching, the page
    renders wrong after a deploy until the user clears their cache by hand.
    no-cache does not mean "do not store" — ETags still allow a 304, so no bandwidth is
    wasted.
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
    """Pick Y-axis gridlines on round numbers that just cover the maximum value.

    Letting the axis ceiling be the raw maximum turns the gridline labels into numbers like
    1,217, against which no other bar can be read — and the axis exists precisely to give a
    value to the bars that carry no label of their own.
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

# Human-readable field names — used by the "fields staff had to correct" chart
FIELD_LABELS = {
    "name": "ชื่อ-นามสกุล", "tel": "เบอร์โทร", "plate_raw": "ทะเบียน", "brand": "ยี่ห้อ",
    "car_type": "ประเภทรถ", "location": "ที่จอด", "deposit_date": "วันที่", "province": "จังหวัด",
}


def _relabel(row: dict, table: dict) -> dict:
    """Swap a raw DB key for its human-readable name, without mutating the original dict"""
    return {**row, "label": table.get(row["label"], row["label"])}


templates.env.filters["to_reason"] = lambda r: _relabel(r, REASON_LABELS)
templates.env.filters["to_field"] = lambda r: _relabel(r, FIELD_LABELS)

FORM_FIELDS = ("name", "tel", "date", "noplate", "province", "brand", "typecar", "location")


def current_user(request: Request) -> User | None:
    token = request.cookies.get(COOKIE_NAME, "")
    return read_token(token, _SECRET) if token else None


# A cookie identifying "which browser this is", used as the owner of a queue claim.
# The session token cannot serve: three people log in as the same staff account, so their
# payloads are identical. It needs no signature, because forging it only lets you take a slip
# you already have access to — it is not a permission boundary.
WORKER_COOKIE = "ocrslip_worker"
WORKER_TTL = 30 * 24 * 3600


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Close every page outside PUBLIC_PATHS, and enforce admin rights on the designated ones"""
    path = request.url.path
    if path.startswith(PUBLIC_PATHS):
        return await call_next(request)

    user = current_user(request)
    if user is None:
        if request.headers.get("HX-Request"):  # for an HTMX request, tell the browser to navigate to login
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
    """Forbid the browser from caching HTML pages.

    This has to be declared *after* auth_gate, because Starlette puts the most recently
    registered middleware outermost. Declared before it, the responses auth_gate returns
    itself (the 303 to login, the 403 page) would never pass through here and so would carry
    no Cache-Control at all.

    Previously no Cache-Control was sent, so browsers (iOS Safari especially) applied
    heuristic caching and decided for themselves that the page was storable. After a deploy
    users were still being served the old version of a form against the new server — and the
    old form lacks fields the new server requires, so submitting it broke immediately.
    The data on these pages changes constantly (the review queue, search results) and is
    personal data, so it should not be sitting in a local cache in any case.
    """
    response = await call_next(request)

    # Do not key off content-type: a 303 redirect has no body and therefore no content-type,
    # so checking for "text/html" would leave the redirect to login without the header.
    # /static is skipped (it carries its own no-cache) along with /image (evidence images never
    # change, so they can be cached for a long time), and a value a route already set is never
    # overwritten.
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
    if not nxt.startswith("/"):  # guard against an open redirect
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


# ---------- the self-service entry form filled in by the driver ----------
# This route involves no OCR and no review queue, because the person who knows the data is
# the person typing it. There is no "can we read this?" question left to ask, which leaves
# the review queue handling only the hand-written slips that arrive as photos.

CAR_TYPES = ("เก๋ง", "กระบะ", "ตู้")

# Provinces promoted to the first group of the dropdown. The site is in Pathum Thani and
# most cars come from nearby. On iOS, picking a province means spinning a wheel, so without
# this promotion every single car means scrolling past dozens of entries.
COMMON_PROVINCES = ("ปทุมธานี", "กรุงเทพมหานคร", "นนทบุรี", "นครปฐม", "สมุทรปราการ",
                    "พระนครศรีอยุธยา", "นครนายก", "สระบุรี")


# Keys in app_settings — defined in exactly one place, because the moment the reading page
# and the writing page spell one differently, a saved value vanishes silently with nothing
# appearing to break.
SETTING_BUILDINGS = "entry_buildings"
SETTING_FLOORS = "entry_floors"
SETTING_QUOTES = "day_quotes"
SETTING_CHECK_REASONS = "check_reasons"

# Reasons offered on /check when none have been set from the settings page. "Other" is not in
# this list: it is always appended by the form itself, so clearing the list in settings can
# never leave a visitor with no way to state why they came.
CHECK_REASONS = ("มาสตาร์ท/เช็คสภาพรถ", "มาเอาของในรถ")
CHECK_OTHER = "__other__"


def _lines(text: str) -> tuple[str, ...]:
    """Turn a textarea into a list of choices, dropping blank lines and surrounding whitespace"""
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _entry_lists(conn) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The buildings and floors actually in use — settable from the web UI, falling back to env.

    Read on every request rather than cached at process start, because the entire reason this
    moved onto the web UI was "an edit takes effect immediately, with no restart". The table
    holds only a handful of rows.
    """
    saved = get_settings(conn)
    return (_lines(saved.get(SETTING_BUILDINGS, "")) or ENTRY_BUILDINGS,
            _lines(saved.get(SETTING_FLOORS, "")) or ENTRY_FLOORS)


def _day_quotes(conn) -> tuple[str, ...]:
    """The quotes on the day colour band — settable from the web UI, falling back to the set shipped in code.

    Read on every registration, as the buildings and floors are, for the same reason: an edit
    has to take effect immediately.
    """
    return _lines(get_settings(conn).get(SETTING_QUOTES, "")) or QUOTES


def _check_reasons(conn) -> tuple[str, ...]:
    """The reasons offered on /check — settable from the web UI, falling back to CHECK_REASONS"""
    return _lines(get_settings(conn).get(SETTING_CHECK_REASONS, "")) or CHECK_REASONS


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
    # Compared with compare_digest rather than ==, so the time taken does not reveal how many
    # leading characters were right. It has to compare bytes: compare_digest rejects a str
    # containing non-ASCII characters, which a Thai-language passcode does.
    # There is deliberately no rate limit here (we agreed to add no further machinery) — the
    # real control is that a staff member asks for the device and types the code themselves;
    # the code is never told to the driver.
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
        # Guard against a repeated submit (page refresh, double-tap) becoming two slips for
        # one car: if this plate already has an uncollected slip registered earlier today,
        # return the existing one. A duplicate hurts on the way out, where staff see two
        # identical rows and cannot tell which to close.
        dup = open_slip_by_plate(conn, norm_plate(v["noplate"]))
        if dup:
            slip = dup
        else:
            # An empty review_reason makes insert_slip set needs_review = false itself
            slip_id = insert_slip(
                conn, fields,
                confidence={}, raw_ocr={}, review_reason=[],
                ocr_model=None, ocr_variant=None,
                # Typed by the driver and attested by staff = nothing left to review, so it
                # goes straight to a searchable state.
                review_status="approved", entry_source="typed",
            )
            conn.commit()
            slip = get_slip(conn, slip_id)

        # The colour band is computed here rather than in the template, because the quotes
        # live in the database and a template cannot open a connection of its own. A slip with
        # no deposit date gets stamp = None and shows no band.
        stamp = day_stamp(slip["deposit_date"], _day_quotes(conn))

    return render(request, "in_done.html", slip=slip, again=bool(dup), stamp=stamp)


# A slip closed through /out has no staff name to record: that page has no login, only a
# shared passcode. Marking the origin plainly beats guessing at a person, and it also keeps
# these separable from slips closed by staff when auditing later.
# released_to is always left empty: that field means "collected by somebody other than the
# owner" (see slip.html). On this route the owner entered their own phone number, so filling
# in the name from the slip would amount to recording that we checked somebody's ID, which
# is not true.
RETURNED_BY_OUT = "ฟอร์มขาออก"
RETURNED_NOTE_OUT = "ผู้มาจอดกรอกเบอร์โทร+ทะเบียนเอง เจ้าหน้าที่ยืนยันด้วยรหัส"


@app.get("/out", response_class=HTMLResponse)
def pickup_form(request: Request):
    if not ENTRY_PASSWORD:
        return HTMLResponse("ยังไม่ได้เปิดใช้ฟอร์มนี้ (ผู้ดูแลระบบยังไม่ได้ตั้ง ENTRY_PASSWORD)",
                            status_code=503)
    return render(request, "out.html", errors={}, v={})


@app.post("/out", response_class=HTMLResponse)
async def pickup_submit(request: Request):
    """Request a car back: the driver enters their phone number and plate, then staff type the closing code.

    The plate is the key, the phone number is the confirmation. Both are compared against the
    columns normalized at insert time (plate_norm strips spaces, dashes and the province;
    tel_digits keeps only digits), so it makes no difference whether somebody types
    "กก 1234", "กก-1234" or "081-234-5678".
    """
    if not ENTRY_PASSWORD:
        return HTMLResponse("ยังไม่ได้เปิดใช้ฟอร์มนี้", status_code=503)

    form = await request.form()
    v = {k: (form.get(k) or "").strip() for k in ("tel", "noplate")}
    tel, plate = norm_phone(v["tel"]), norm_plate(v["noplate"])
    slip_id = (form.get("slip_id") or "").strip()

    errors: dict[str, str] = {}
    if len(tel) != 10:
        errors["tel"] = "เบอร์โทรต้องเป็นตัวเลข 10 หลัก"
    if not plate:
        errors["noplate"] = "กรอกทะเบียนรถ"
    # Check the code *before* touching the database, and reject immediately if it is wrong —
    # not search first and check afterwards. Otherwise /out becomes a tool for outsiders to
    # probe which plates are parked here without knowing the code at all (the messages "no
    # slip found" and "phone number does not match" already give away half the answer).
    # Compared with compare_digest over bytes, as on the way in, for the same reason.
    if not secrets.compare_digest(
        (form.get("entry_pw") or "").encode(), ENTRY_PASSWORD.encode()
    ):
        errors["entry_pw"] = "รหัสเจ้าหน้าที่ไม่ถูกต้อง"
    if errors:
        return render(request, "out.html", errors=errors, v=v)

    with connect() as conn:
        rows = slips_by_plate(conn, plate)
        stored = [r for r in rows if r["car_status"] == "stored"]
        # The phone number is confirmation, not a key: among the slips that came from photos,
        # some have a phone number OCR could not read. Requiring a match on every slip would
        # block the genuine owner over data we ourselves could not read. Where a slip *does*
        # hold a phone number it must match exactly — and the plate and the staff passcode are
        # always required regardless.
        match = [r for r in stored if not (r["tel_digits"] or "") or r["tel_digits"] == tel]

        if not rows:
            errors["noplate"] = "ไม่พบใบจอดของทะเบียนนี้ — ตรวจตัวอักษรกับตัวเลขอีกครั้ง"
        elif not stored:
            last = rows[0]
            when = (last["returned_at"].strftime("%d/%m/%Y %H:%M")
                    if last["returned_at"] else "ก่อนหน้านี้")
            errors["noplate"] = f"ใบของทะเบียนนี้รับรถกลับไปแล้วเมื่อ {when}"
        elif not match:
            errors["tel"] = "เบอร์โทรไม่ตรงกับใบจอดของทะเบียนนี้"
        if errors:
            return render(request, "out.html", errors=errors, v=v)

        # slip_id arrives from the slip-picker page, so it must be looked up within match and
        # never trusted as submitted. Otherwise anyone who knows the passcode could post
        # somebody else's slip id and close it while entering a different plate.
        chosen = next((r for r in match if str(r["id"]) == slip_id), None)
        if chosen is None and len(match) > 1:
            # One plate can have several still-parked slips (parked again over an older slip,
            # or a duplicate that escaped the duplicate check). We cannot guess on their
            # behalf: close the wrong one and a slip is stranded that nobody will come to
            # collect, so let them choose.
            # Ordered by deposit date, newest first — not by the order slips were recorded in
            # the system. The current round's slip is the one most people are here to collect,
            # so it has to sit at the top where the eye lands first. (A slip photographed into
            # the system later may well belong to an older round; the two orderings are not
            # the same thing.)
            picks = sorted(match, key=lambda r: (r["deposit_date"] is not None,
                                                 r["deposit_date"]), reverse=True)
            return render(request, "out_pick.html", v=v, picks=picks,
                          rounds=deposit_rounds(conn, [plate]))
        chosen = chosen or match[0]

        ok = mark_returned(conn, str(chosen["id"]), RETURNED_BY_OUT, RETURNED_NOTE_OUT)
        if ok:
            # Somebody who went in to check the car and then decided to drive it away never
            # comes back through /check/out. Leaving that visit open would list them as
            # "still at the car" forever, so leaving with the car closes it too.
            finish_car_checks(conn, str(chosen["id"]), RETURNED_BY_OUT)
        conn.commit()
        if not ok:
            # mark_returned returning False means the slip was already closed (staff pressed
            # it from the slip page, or this is a repeat press). That has to be visible, not
            # dressed up as a fresh success with a summary page.
            errors["noplate"] = "ใบนี้ถูกปิดไปแล้วเมื่อครู่นี้ — สอบถามเจ้าหน้าที่"
            return render(request, "out.html", errors=errors, v=v)

        slip = get_slip(conn, str(chosen["id"]))
        # The band shows the *collection* day, not the deposit day — this screenshot is
        # evidence of the day the car left.
        stamp = day_stamp(slip["returned_at"], _day_quotes(conn))

    return render(request, "out_done.html", slip=slip, stamp=stamp)


# ---------- a visit to a car that stays parked (/check, /check/out) ----------
# Some owners do not want the car back yet; they come to start it, check on it, or take
# something out of it. They still have to pass the screening point on the way to the car and
# again on the way back, so this runs the same gate as /out (plate + phone number + staff
# passcode) twice — /check when they go in, /check/out when they come back — and closes no slip:
# the slip stays 'stored', and each visit is one row in car_checks with a start and a finish.

CHECKED_BY_FORM = "ฟอร์มเช็ครถ"


def _check_form(request: Request, conn, errors: dict, v: dict) -> HTMLResponse:
    return render(request, "check.html", errors=errors, v=v,
                  reasons=_check_reasons(conn), other=CHECK_OTHER)


def _parked_match(conn, tel: str, plate: str, errors: dict) -> list[dict[str, Any]]:
    """The still-parked slips matching this plate and phone, filling errors when there are none.

    Shared by check-in and check-out so the two can never disagree about which car is meant.
    The phone number is confirmation, not a key — same rule as /out, since some photographed
    slips have a phone number OCR could not read.
    """
    rows = slips_by_plate(conn, plate)
    stored = [r for r in rows if r["car_status"] == "stored"]
    match = [r for r in stored if not (r["tel_digits"] or "") or r["tel_digits"] == tel]
    if not rows:
        errors["noplate"] = "ไม่พบใบจอดของทะเบียนนี้ — ตรวจตัวอักษรกับตัวเลขอีกครั้ง"
    elif not stored:
        errors["noplate"] = "ใบของทะเบียนนี้รับรถกลับไปแล้ว — รถไม่ได้จอดอยู่ที่นี่"
    elif not match:
        errors["tel"] = "เบอร์โทรไม่ตรงกับใบจอดของทะเบียนนี้"
    return match


def _bad_pw(form) -> bool:
    # Checked before any lookup, as on /out: a wrong code must reveal nothing about which plates
    # are parked here.
    return not secrets.compare_digest(
        (form.get("entry_pw") or "").encode(), ENTRY_PASSWORD.encode())


@app.get("/check", response_class=HTMLResponse)
def check_form(request: Request):
    if not ENTRY_PASSWORD:
        return HTMLResponse("ยังไม่ได้เปิดใช้ฟอร์มนี้ (ผู้ดูแลระบบยังไม่ได้ตั้ง ENTRY_PASSWORD)",
                            status_code=503)
    with connect() as conn:
        return _check_form(request, conn, {}, {})


@app.post("/check", response_class=HTMLResponse)
async def check_submit(request: Request):
    if not ENTRY_PASSWORD:
        return HTMLResponse("ยังไม่ได้เปิดใช้ฟอร์มนี้", status_code=503)

    form = await request.form()
    v = {k: (form.get(k) or "").strip() for k in ("tel", "noplate", "reason", "reason_other")}
    tel, plate = norm_phone(v["tel"]), norm_plate(v["noplate"])

    with connect() as conn:
        reasons = _check_reasons(conn)
        # The chosen reason must be one of the offered ones (or "other" with text): a free value
        # posted straight in would make the reasons impossible to count later.
        reason = v["reason_other"] if v["reason"] == CHECK_OTHER else v["reason"]

        errors: dict[str, str] = {}
        if len(tel) != 10:
            errors["tel"] = "เบอร์โทรต้องเป็นตัวเลข 10 หลัก"
        if not plate:
            errors["noplate"] = "กรอกทะเบียนรถ"
        if v["reason"] == CHECK_OTHER and not v["reason_other"]:
            errors["reason"] = "พิมพ์เหตุผลที่มา"
        elif v["reason"] != CHECK_OTHER and v["reason"] not in reasons:
            errors["reason"] = "เลือกเหตุผลที่มา"
        if _bad_pw(form):
            errors["entry_pw"] = "รหัสเจ้าหน้าที่ไม่ถูกต้อง"
        if errors:
            return _check_form(request, conn, errors, v)

        match = _parked_match(conn, tel, plate, errors)
        # A visit already in progress means the last one was never checked out. Opening a second
        # would leave two "still at the car" rows for one person; staff close the old one first.
        if not errors and open_car_check(conn, [str(r["id"]) for r in match]):
            errors["noplate"] = ("ทะเบียนนี้เข้าเช็ครถอยู่แล้ว ยังไม่ได้แจ้งออก — "
                                 "ให้กรอกฟอร์มเช็ครถขาออกก่อน แล้วค่อยเข้าใหม่")
        if errors:
            return _check_form(request, conn, errors, v)

        # Unlike /out there is no slip picker: nothing is being closed, so linking the visit to
        # the most recent deposit among several open slips cannot strand anything.
        slip = max(match, key=lambda r: (r["deposit_date"] is not None, r["deposit_date"],
                                         r["created_at"]))
        check = add_car_check(conn, str(slip["id"]), v["tel"], v["noplate"], reason,
                              CHECKED_BY_FORM)
        conn.commit()
        stamp = day_stamp(check["created_at"], _day_quotes(conn))

    return render(request, "check_done.html", slip=slip, check=check, stamp=stamp)


@app.get("/check/out", response_class=HTMLResponse)
def check_out_form(request: Request):
    if not ENTRY_PASSWORD:
        return HTMLResponse("ยังไม่ได้เปิดใช้ฟอร์มนี้ (ผู้ดูแลระบบยังไม่ได้ตั้ง ENTRY_PASSWORD)",
                            status_code=503)
    return render(request, "check_out.html", errors={}, v={})


@app.post("/check/out", response_class=HTMLResponse)
async def check_out_submit(request: Request):
    """The owner is back from the car: close the visit. The slip itself stays open."""
    if not ENTRY_PASSWORD:
        return HTMLResponse("ยังไม่ได้เปิดใช้ฟอร์มนี้", status_code=503)

    form = await request.form()
    v = {k: (form.get(k) or "").strip() for k in ("tel", "noplate")}
    tel, plate = norm_phone(v["tel"]), norm_plate(v["noplate"])

    errors: dict[str, str] = {}
    if len(tel) != 10:
        errors["tel"] = "เบอร์โทรต้องเป็นตัวเลข 10 หลัก"
    if not plate:
        errors["noplate"] = "กรอกทะเบียนรถ"
    if _bad_pw(form):
        errors["entry_pw"] = "รหัสเจ้าหน้าที่ไม่ถูกต้อง"
    if errors:
        return render(request, "check_out.html", errors=errors, v=v)

    with connect() as conn:
        match = _parked_match(conn, tel, plate, errors)
        visit = None if errors else open_car_check(conn, [str(r["id"]) for r in match])
        if not errors and visit is None:
            errors["noplate"] = "ทะเบียนนี้ไม่มีการเข้าเช็ครถที่ค้างอยู่ — อาจแจ้งออกไปแล้ว"
        if errors:
            return render(request, "check_out.html", errors=errors, v=v)

        closed = finish_car_checks(conn, str(visit["slip_id"]), CHECKED_BY_FORM)
        conn.commit()
        if not closed:
            # Someone else closed it between the lookup and the update
            errors["noplate"] = "การเข้าเช็ครถนี้ถูกแจ้งออกไปแล้วเมื่อครู่นี้"
            return render(request, "check_out.html", errors=errors, v=v)
        check = closed[0]
        slip = get_slip(conn, str(check["slip_id"]))
        stamp = day_stamp(check["finished_at"], _day_quotes(conn))

    return render(request, "check_out_done.html", slip=slip, check=check, stamp=stamp)


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
    """Upload many slips at once — every one lands in the pending queue awaiting a reviewer.

    Processed in parallel, because almost all the time is spent waiting on the LLM to answer
    (backfilling hundreds of slips one at a time would be too slow to use).
    """
    # created_by = the logged-in account (unforgeable); uploaded_by/photographer = the real
    # person's name that was selected.
    account = getattr(request.state, "user", None) and request.state.user.username
    # The value "__new__" means the user picked "+ new name" in the list and typed into the
    # adjacent box.
    def pick(choice: str, typed: str) -> str | None:
        raw = typed if choice.strip() == "__new__" else choice
        # clean_person_name strips invisible characters and rejects the sentinel value;
        # otherwise we get a name that reads as empty to a human while the system counts it.
        return clean_person_name(raw) or None

    uploader = pick(uploaded_by, uploaded_by_new)
    shooter = pick(photographer, photographer_new) or uploader

    # This has to be checked server-side too, because HTML's required attribute is bypassed
    # by posting to the API directly. Let it through and we get a slip with no record of who
    # entered it — precisely what this feature exists to prevent.
    if not uploader:
        return render(request, "partials/upload_result.html", results=[],
                      uploader=None, shooter=None,
                      error="ต้องระบุชื่อคนอัปโหลดก่อน จะได้รู้ว่าใบนี้ใครเป็นคนบันทึก")
    # A name not yet on the list is simply added, so nobody on the ground is blocked during an
    # emergency. An admin can deactivate or tidy them up later on the /staff page.
    with connect() as conn:
        for person in {uploader, shooter}:
            if person:
                add_staff(conn, person, created_by=account)
        conn.commit()

    dropped: list[str] = []
    uploads = [(f.filename, await f.read()) for f in files]
    uploads = [(name, raw) for name, raw in uploads if raw]
    # The same file attached twice within one request has to be dropped here: the duplicate
    # gate inside ingest reads from the database, but every slip in a batch is processed in
    # parallel and committed at the end, so none of them can see the others.
    # (Production showed 7 such groups less than 10 seconds apart = self-duplicates within a
    # single request.)
    seen: set[str] = set()
    fresh = []
    for name, raw in uploads:
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            dropped.append(name)
            continue
        seen.add(digest)
        fresh.append((name, raw))
    uploads = fresh

    def one(item: tuple[str, bytes]) -> dict[str, Any]:
        name, raw = item
        try:
            with connect() as conn:  # one connection per thread
                return {**ingest(conn, raw, created_by=account,
                                 uploaded_by=uploader, photographer=shooter), "filename": name}
        except Exception as exc:  # one failed slip must not bring down the whole batch
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "filename": name}

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(uploads)))) as pool:
        results = list(pool.map(one, uploads))
    return render(request, "partials/upload_result.html", results=results,
                  uploader=uploader, shooter=shooter, dropped=dropped)


# The piles an approver may open — only the work still outstanding, not the whole archive of
# past slips. "dup" is included, because reviewers are the people who need to see where slips
# that vanished from the queue went. Hidden from everyone, a wrongly-flagged duplicate would
# be impossible to ever discover.
APPROVER_FILTERS = ("needs", "quick", "dup")

# Each "pile" is a preset over the same filter set the table page uses, not a separate query.
# The to-do piles must exclude duplicates (superseded=False), or reviewers are handed slips a
# colleague has already reviewed.
QUEUE_PILES = {
    "needs": dict(review_status="pending", needs_review=True, superseded=False),
    "quick": dict(review_status="pending", needs_review=False, superseded=False),
    "dup": dict(superseded=True),
    "approved": dict(review_status="approved"),
    "rejected": dict(review_status="rejected"),
    "all": {},
}

# Which review_counts() column holds each pile's total — used instead of recounting when
# nothing is being searched.
PILE_TOTALS = {"needs": "needs_review", "quick": "quick_pass", "dup": "superseded",
               "approved": "approved", "rejected": "rejected", "all": "total"}


@app.get("/review", response_class=HTMLResponse)
def review_queue(
    request: Request, filter: str = "needs",
    q: str = "", sort: str = "created_at", dir: str = "desc", page: int = 1,
):
    """The review queue — sharing the filter, search, sort and pagination machinery with the table page.

    The selected pile always overrides the query, so an approver can search without escaping
    the piles they are allowed to see.
    """
    user = getattr(request.state, "user", None)
    if user and user.is_approver and filter not in APPROVER_FILTERS:
        filter = "needs"
    pile = QUEUE_PILES.get(filter, {})
    filters = build_filters(q=q, **pile)
    per_page = 50
    with connect() as conn:
        # With nothing searched, each pile's total already sits in review_counts(), which has
        # to be queried for the pile tabs anyway — so query_slips need not count again. At
        # 33,000 slips that is the difference between 0.2 ms and 96 ms.
        counts = review_counts(conn)
        known_total = PILE_TOTALS.get(filter) if not (q and q.strip()) else None
        slips, total = query_slips(conn, filters=filters, sort=sort,
                                   desc=(dir != "asc"), page=max(1, page), per_page=per_page,
                                   total=counts[known_total] if known_total else None)
    qs = urlencode({k: v for k, v in
                    {"filter": filter, "q": q, "sort": sort, "dir": dir}.items() if v})
    return render(
        request, "review_list.html", slips=slips, counts=counts, active=filter,
        # Unreviewed piles have no owner yet, so the reviewer column would be empty on every row
        show_reviewer=(filter in ("approved", "rejected", "all")),
        total=total, page=max(1, page), per_page=per_page,
        pages=max(1, -(-total // per_page)), qs=qs,
        f={"q": q, "sort": sort, "dir": dir},
    )


@app.get("/review/next")
def review_next(request: Request):
    """Claim whatever is next in the queue right now, then navigate there.

    The "next slip" button used to follow an id computed at render time, which is a snapshot
    of the queue a moment ago rather than now — so a slower reviewer walking that stale id
    would land on the slip a colleague had just finished. This asks the database live at press
    time and claims the slip in the same statement, so nobody else gets it.
    """
    with connect() as conn:
        nxt = claim_next(conn, request.state.worker)
        conn.commit()
    return RedirectResponse(f"/review/{nxt}" if nxt else "/review", status_code=303)


@app.get("/review/{slip_id}", response_class=HTMLResponse)
def review_one(request: Request, slip_id: str, edit: int = 0):
    """The single-slip review page — edit=1 is an explicit confirmation to edit an already-reviewed slip.

    The queue hands the same head-of-line slip to everyone (next_in_queue), and the "next
    slip" button follows an id computed when the page loaded, so a slower reviewer can always
    land on the slip a colleague has just finished. Rendering the form normally would
    preselect the reviewer field with the name of whoever already reviewed it (the template
    lets the DB value beat the locally remembered one), and pressing approve from there would
    overwrite a colleague's work on the spot.
    """
    with connect() as conn:
        slip = get_slip(conn, slip_id)
        if not slip:
            return HTMLResponse("ไม่พบใบนี้", status_code=404)
        # Opening a slip claims it, covering the case where somebody clicked through from the
        # queue list rather than pressing the next-slip button. Failing to claim means
        # somebody else holds it — a warning, not a block, because it may be the same person
        # on another device, and the real guard is require_status at approval time anyway.
        held = claim_one(conn, slip_id, request.state.worker)
        conn.commit()
        # Next in the queue: slips needing review first, then the ones merely awaiting a confirm
        next_id, remaining = next_in_queue(conn, slip_id)
        return render(
            request, "review_detail.html",
            slip=slip, problems=(slip.get("raw_ocr") or {}).get("problems", {}),
            next_id=next_id, remaining=remaining,
            people=known_people(conn),
            taken=(slip["review_status"] != "pending" and not edit),
            held_by=held,
            # Self-service slips have no evidence image, which has to be known before render
            # as on the /slips page, or the left half of the page is a broken image frame.
            # And ever since the "edit this slip" link appeared, these are the slips opened
            # most often of all.
            has_image=get_image(conn, slip_id) is not None, car_types=CAR_TYPES,
        )


def _form_fields(form) -> dict[str, Any]:
    return {f: (form.get(f) or "").strip() or None for f in FORM_FIELDS}


def _picked_reviewer(form) -> str | None:
    """The real person's name chosen in the reviewer field ("__new__" = they want to type a new one).

    The staff and approve accounts are shared by several people, so recording the account name
    would make it impossible to trace who reviewed which slip.
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
    # A real person's name is required, as at upload time, so we know who approved this slip
    reviewer = _picked_reviewer(form)
    force = (form.get("force") or "") == "1"
    # Re-validate after the human edit, but block only on *required fields left empty*.
    # Odd formats (a plate with no letter group, a date written as just '26') are warnings
    # only, because the reviewer is looking at the actual slip, and slips really do look like
    # that sometimes.
    reasons, problems = evaluate(fields, {})
    blocking = {f: msg for f, msg in problems.items() if not fields.get(f)}
    with connect() as conn:
        # Checked server-side too, because HTML's required attribute is bypassed by posting directly
        if blocking or not reviewer:
            slip = get_slip(conn, slip_id)
            if not slip:  # the slip was deleted or rejected while the reviewer sat on the page
                return HTMLResponse("ไม่พบใบนี้", status_code=404)
            return render(
                request, "review_detail.html",
                slip={**slip, **{k: v for k, v in fields.items() if v}},
                problems=blocking, next_id=None, remaining=0, people=known_people(conn),
                has_image=get_image(conn, slip_id) is not None, car_types=CAR_TYPES,
                error=("ยังมีช่องบังคับที่ว่างอยู่ (ชื่อ / เบอร์โทร / ทะเบียน) กรอกให้ครบก่อนอนุมัติ"
                       if blocking else
                       "ต้องระบุชื่อผู้ตรวจก่อน จะได้รู้ว่าใบนี้ใครเป็นคนอนุมัติ"),
            )
        # A name not yet on the list is simply added, so reviewers are not blocked during a rush
        add_staff(conn, reviewer, created_by=account)
        # require_status stops two people sitting on the same slip from overwriting each other
        # unnoticed. A deliberate return to edit one's own slip comes in via ?edit=1, which is
        # allowed past this guard.
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
                has_image=get_image(conn, slip_id) is not None, car_types=CAR_TYPES,
            )
        # This slip has now been reviewed, so the remaining copies of it need no review.
        # It has to sit in the same transaction as the approval: fail partway through and you
        # get slips pulled out of the queue with none of them approved — those slips would
        # silently disappear from the workload.
        superseded = mark_superseded(conn, slip_id)
        if superseded:
            print(f"[dedup] approve slip={slip_id} pulled {superseded} duplicate(s) from the queue")
        conn.commit()
    # The next_id embedded in the form is no longer used: it is a snapshot of the queue when
    # the page loaded, which may be many minutes old. /review/next claims a slip live, right
    # now, so nobody else gets the same one.
    return RedirectResponse("/review/next", status_code=303)


@app.post("/review/{slip_id}/reject")
async def reject(request: Request, slip_id: str):
    form = await request.form()
    # The reviewer name must pass through the same filter as on approval, or the sentinel
    # value "__new__" is stored as a person's name and shows up in the statistics as somebody
    # called __new__ having rejected a pile of slips.
    reviewer = _picked_reviewer(form)
    with connect() as conn:
        reject_slip(conn, slip_id, (form.get("reason") or "รูปอ่านไม่ได้"), reviewer)
        conn.commit()
    return RedirectResponse("/review", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, deleted: str = ""):
    return render(request, "search.html", deleted=deleted)


@app.get("/api/search", response_class=HTMLResponse)
def api_search(request: Request, q: str = "", include_pending: bool = False):
    with connect() as conn:
        rows = fuzzy_search(conn, q, include_pending=include_pending) if q else []
        # A car parked over several rounds shows up as several near-identical rows, so each
        # row has to say which round it is. Asked once for every plate in the result set
        # rather than per row (this endpoint fires on every keystroke).
        rounds = deposit_rounds(conn, [r.get("plate_norm") for r in rows])
    return render(request, "partials/results.html", rows=rows, q=q, rounds=rounds)


@app.get("/slips/{slip_id}", response_class=HTMLResponse)
def slip_detail(request: Request, slip_id: str, taken: int = 0):
    with connect() as conn:
        slip = get_slip(conn, slip_id)
        if not slip:
            return HTMLResponse("ไม่พบใบนี้", status_code=404)
        # This once carried a hardcoded schema name of ocr_dhammakaya, which made the slip
        # page 500 whenever DB_SCHEMA was set to anything else (running the tests, for
        # instance). Moved to the db.py helper.
        edits = list_edits(conn, slip_id)
        # Self-service slips have no evidence image, which has to be known before render or
        # the page is left with a broken image frame.
        has_image = get_image(conn, slip_id) is not None
        # This page is where a car gets released, so if this plate has parked over several
        # rounds, every round has to be visible right here — not something to go back and
        # work out on the search page to check the right round is being closed.
        # A lone slip needs no deposit-history card whose single row is the open slip itself.
        history = deposit_history(conn, slip["plate_norm"])
        history = history if len(history) > 1 else []
        checks = list_car_checks(conn, slip_id)
    return render(request, "slip.html", slip=slip, edits=edits, has_image=has_image,
                  taken=bool(taken), history=history, checks=checks)


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
        if ok:  # same as /out: leaving with the car ends any visit still open on it
            finish_car_checks(conn, slip_id, form.get("returned_by") or None)
        conn.commit()
    # A failed close means somebody closed it first, which has to be made plain rather than
    # silently redirecting back as though it had succeeded — otherwise staff never learn that
    # this car may already have been released to somebody else.
    return RedirectResponse(f"/slips/{slip_id}" + ("" if ok else "?taken=1"), status_code=303)


@app.post("/slips/{slip_id}/delete")
async def do_delete(request: Request, slip_id: str):
    """Delete a slip permanently — for test slips and nonsense entries, which only skew the summary figures"""
    account = getattr(request.state, "user", None) and request.state.user.username
    with connect() as conn:
        gone = delete_slip(conn, slip_id)
        conn.commit()
    if not gone:
        return HTMLResponse("ไม่พบใบนี้ (อาจถูกลบไปแล้ว)", status_code=404)
    # There is no audit table for deletions, because slip_edits is cascaded away with the slip
    # anyway. So this is recorded in the process log instead — Render retains logs, which keeps
    # a trace of who deleted which slip and when.
    print(f"[delete] slip={gone['id']} plate={gone.get('plate_raw')!r} "
          f"name={gone.get('name')!r} car_status={gone.get('car_status')} by={account}")
    return RedirectResponse(
        f"/search?deleted={quote(str(gone.get('plate_raw') or gone['id']))}", status_code=303)

# ---------- the page for comparing duplicates that were both already approved ----------
#
# Queued duplicates are pulled out automatically at approval time, but a pair that is already
# approved on both sides cannot be touched automatically: a reviewer did the work on both, and
# the values usually disagree (the model reads the same slip differently on two passes). That
# means a wrong row is sitting in the archive, and somebody has to look at the photo and say
# which one is right. A machine cannot.
#
# So this page decides nothing. Its job is to lay the evidence out so a decision is fast: one
# photo of the slip with every copy's values side by side, the conflicting fields highlighted,
# and the right one kept in a single click. One group at a time rather than a long list,
# because this work is the same judgement repeated hundreds of times.

# The fields to compare — a field that differs means one of the copies was misread
DUP_FIELDS = (("name", "ชื่อ-นามสกุล"), ("tel", "เบอร์โทร"), ("plate_raw", "ทะเบียน"),
              ("province", "จังหวัด"), ("brand", "ยี่ห้อ"), ("car_type", "ประเภทรถ"),
              ("location", "ที่จอด"), ("deposit_date", "วันที่ฝาก"))
# Fields where differing is normal (different uploader, different time). Shown to aid the
# decision, but never counted as a conflict.
DUP_META = (("review_status", "สถานะตรวจ"), ("reviewed_by", "คนตรวจ"),
            ("uploaded_by", "คนอัปโหลด"), ("car_status", "สถานะรถ"),
            ("created_at", "เข้าระบบเมื่อ"))


# The table's column names do not match the slip field names NORMALIZERS knows about
DUP_NORM_KEY = {"plate_raw": "noplate", "car_type": "typecar", "deposit_date": "date"}


def _cell(slip: dict, field: str) -> str:
    return str(slip.get(field) or "").strip()


def _norm_cell(slip: dict, field: str) -> str:
    """The value used for *comparing* — distinct from _cell, which is the value for *display*.

    It has to pass through the same normalizer the system uses to decide whether two slips are
    duplicates. Otherwise this page reports a conflict over nothing but a space, handing a
    person group after group with no decision in them.
    Representative pairs: '1ขก1111' against '1ขก 1111', and '0890000127' against
    '089 000 0127' — cases where plate_norm and tel_digits in the archive are already
    byte-identical, meaning the two slips do not differ at all.
    """
    return normalize_field(DUP_NORM_KEY.get(field, field), slip.get(field))


def _clashes(group: list[dict[str, Any]]) -> set[str]:
    """Fields on which the group's slips disagree. Empty = they are all identical, so keep any one."""
    return {f for f, _ in DUP_FIELDS if len({_norm_cell(s, f) for s in group}) > 1}


def _identical(groups: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Groups where keeping any copy gives the same result — no field conflicts, and one shared car status.

    Car status has to be checked even though OCR never reads it: if a group holds both a
    still-parked slip and a collected one, picking the wrong copy changes the answer to "is
    this car still here?" — a question for a person, not for a bulk-resolve button.
    """
    return [g for g in groups
            if not _clashes(g) and len({s["car_status"] for s in g}) == 1]


def _dup_groups(conn) -> list[list[dict[str, Any]]]:
    """Duplicate groups holding more than one approved slip, oldest group first.

    Both the groups and their members are given a stable order, because this page navigates by
    group index (?i=). If the order shuffled between requests, the "skip this group" button
    would land on the group just skipped.
    """
    groups = [g for g in group_duplicates(load_slips(conn, only_candidates=True))
              if len([s for s in g if s["review_status"] == "approved"]) > 1]
    for g in groups:
        g.sort(key=lambda s: s["created_at"])
    groups.sort(key=lambda g: g[0]["created_at"])
    return groups


@app.get("/dups", response_class=HTMLResponse)
def dups_page(request: Request, i: int = 0, done: int = 0):
    with connect() as conn:
        groups = _dup_groups(conn)
    if not groups:
        return render(request, "dups.html", group=None, total=0, index=0, done=done)
    index = min(max(0, i), len(groups) - 1)
    group = groups[index]
    same = _identical(groups)
    return render(
        request, "dups.html", group=group, total=len(groups), index=index, done=done,
        fields=DUP_FIELDS, meta=DUP_META,
        # The bulk-resolve button for groups with no decision in them — in production 56 of
        # 273 groups were like this. Without a way to resolve them in bulk, somebody has to
        # click through them one at a time, deciding nothing.
        identical_groups=len(same), identical_slips=sum(len(g) - 1 for g in same),
        # The fields they disagree on are where the decision lies; the rest can be skimmed past
        conflicts=_clashes(group),
        # One shared photo = show it once, rather than making somebody look at the same image three times
        one_photo=(len({s["osha"] for s in group}) == 1 and group[0]["osha"] is not None),
        # Keeping the collected copy while another is still parked leaves that car with no active slip
        mixed_car=(len({s["car_status"] for s in group}) > 1),
    )


@app.post("/dups/resolve-identical")
async def dups_resolve_identical(request: Request):
    """Resolve every group whose copies are wholly identical, keeping the oldest.

    Doing this in one pass is safe precisely because there is nothing to decide: every stored
    field agrees and the car status agrees, so whichever copy is kept leaves the archive
    character-for-character the same. The oldest is chosen because it is the one the first
    reviewer worked on.
    Any group with even one conflicting field is left untouched — that is the work that needs
    a person looking at the photo.
    """
    account = getattr(request.state, "user", None) and request.state.user.username
    removed = 0
    with connect() as conn:
        for group in _identical(_dup_groups(conn)):
            for extra in group[1:]:      # already oldest-first, so the first one is the keeper
                gone = delete_slip(conn, extra["id"])
                if gone:
                    removed += 1
                    print(f"[dups] resolved an identical group keep={group[0]['id']} "
                          f"drop={gone['id']} plate={gone.get('plate_raw')!r} by={account}")
        conn.commit()
    return RedirectResponse(f"/dups?done={removed}", status_code=303)


@app.post("/dups/resolve")
async def dups_resolve(request: Request):
    """Keep the copy the person chose, delete the rest of that group.

    The group is recomputed server-side from the chosen id rather than trusting the list of
    ids the form submitted, so a tampered form cannot order the deletion of slips outside that
    group. (This button deletes permanently.)
    """
    form = await request.form()
    keep = str(form.get("keep") or "")
    account = getattr(request.state, "user", None) and request.state.user.username
    try:
        index = max(0, int(str(form.get("i") or 0)))
    except ValueError:
        index = 0
    with connect() as conn:
        group = next((g for g in _dup_groups(conn) if any(s["id"] == keep for s in g)), None)
        if group is None:
            return HTMLResponse("กลุ่มนี้ถูกจัดการไปแล้ว (หรือใบที่เลือกถูกลบไปก่อน)",
                                status_code=404)
        gone = [delete_slip(conn, s["id"]) for s in group if s["id"] != keep]
        conn.commit()
    for row in gone:
        if row:
            print(f"[dups] keep={keep} ลบใบซ้ำ slip={row['id']} plate={row.get('plate_raw')!r} "
                  f"name={row.get('name')!r} by={account}")
    return RedirectResponse(f"/dups?i={index}&done={len(gone)}", status_code=303)


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
        quotes = _day_quotes(conn)
        reasons = _check_reasons(conn)
    return render(
        request, "settings.html",
        buildings="\n".join(buildings), floors="\n".join(floors),
        quotes="\n".join(quotes), quote_days=quote_cycle_days(quotes),
        check_reasons="\n".join(reasons),
        # State plainly where each displayed value came from. Otherwise an admin cannot tell
        # whether they are looking at the env default or at a value they set themselves, and
        # may clear it believing "clearing it leaves the same value anyway".
        from_db={"buildings": SETTING_BUILDINGS in stored, "floors": SETTING_FLOORS in stored,
                 "quotes": SETTING_QUOTES in stored,
                 "check_reasons": SETTING_CHECK_REASONS in stored},
        entry_open=bool(ENTRY_PASSWORD), saved=bool(saved))


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    account = getattr(request.state, "user", None) and request.state.user.username
    with connect() as conn:
        set_setting(conn, SETTING_BUILDINGS, str(form.get("buildings") or ""), account)
        set_setting(conn, SETTING_FLOORS, str(form.get("floors") or ""), account)
        set_setting(conn, SETTING_QUOTES, str(form.get("quotes") or ""), account)
        set_setting(conn, SETTING_CHECK_REASONS, str(form.get("check_reasons") or ""), account)
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
