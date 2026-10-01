"""ฟอร์มขาออกที่ผู้มาจอดกรอกเอง (/out)

หน้านี้เปิดให้คนนอกเข้าได้เหมือน /in และปิดใบจอด (ปล่อยรถ) ได้จริง เทสต์ที่สำคัญที่สุด
ในไฟล์นี้มีสองข้อ:
  * รหัสเจ้าหน้าที่ต้องไม่โผล่ออกไปกับหน้าเว็บ — ถ้าหลุด ใครก็ปล่อยรถคนอื่นได้จากที่บ้าน
  * รหัสผิดต้องไม่บอกอะไรเลยเกี่ยวกับใบ — ไม่งั้น /out กลายเป็นเครื่องมือยิงถามว่า
    ทะเบียนไหนจอดอยู่ที่นี่บ้าง โดยไม่ต้องรู้รหัส
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip.web import main
from ocrslip.web.main import app

from conftest import TEST_SCHEMA, needs_db

PW = "รหัสทดสอบ-xyz"

# ใบขาเข้าที่ใช้ตั้งต้นทุกเทสต์ในไฟล์นี้ — ผ่าน /in จริง ไม่ใช่ยัดแถวเข้าฐานข้อมูลเอง
# เพราะของที่ทดสอบคือ "ขาออกหาใบที่ขาเข้าสร้างไว้เจอไหม" ถ้ายัดแถวเองจะไม่เจอบั๊ก
# ตรงที่สองฝั่ง normalize ไม่เหมือนกัน
IN = {
    "name": "สมชาย ใจดี", "tel": "081-234-5678", "date": "2026-09-28",
    "noplate": "1กก 1234", "province": "ปทุมธานี", "brand": "โตโยต้า",
    "typecar": "เก๋ง", "building": "อาคาร 1", "floor": "ชั้น 2",
    "entry_pw": PW,
}
OUT = {"tel": "0812345678", "noplate": "1กก1234", "entry_pw": PW}


@pytest.fixture
def pw(pgenv, monkeypatch):
    monkeypatch.setattr(main, "ENTRY_PASSWORD", PW)


@pytest.fixture
def clean(pgenv):
    """ฐานข้อมูลว่าง + ตัวเลือกอาคาร/ชั้นกลับไปเป็นค่าตั้งต้นของโค้ด

    ต้องล้าง app_settings ด้วย ไม่ใช่แค่ slips: ไฟล์นี้สร้างใบตั้งต้นด้วยการยิง /in จริง
    ซึ่งตรวจว่าอาคาร/ชั้นที่ส่งมาอยู่ในลิสต์ที่อ่านจาก app_settings — ถ้าเทสต์ไฟล์อื่น
    (test_entry_settings) ทิ้งลิสต์ของมันไว้ในตารางนั้น /in จะตีกลับว่า "เลือกอาคารที่จอด"
    แล้วเทสต์ในไฟล์นี้จะล้มทั้งแถบเพราะเหตุที่ไม่เกี่ยวกับขาออกเลย — และล้มเฉพาะเวลารัน
    ทั้ง suite เท่านั้น ซึ่งเป็นความล้มแบบที่ไล่หาสาเหตุยากที่สุด
    """
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.app_settings")
        conn.commit()


def _row():
    from ocrslip.db import connect
    with connect() as conn:
        return conn.execute(f"SELECT * FROM {TEST_SCHEMA}.slips").fetchone()


# ---------- ด่านของหน้านี้ ----------

def test_page_is_off_when_no_password_configured(monkeypatch):
    """ไม่ได้ตั้งรหัส = ปิดหน้าไปเลย ดีกว่าเปิดให้ปล่อยรถโดยไม่มีด่านอะไรเลย"""
    monkeypatch.setattr(main, "ENTRY_PASSWORD", "")
    assert TestClient(app).get("/out").status_code == 503
    assert TestClient(app).post("/out", data=OUT).status_code == 503


def test_open_to_public_without_login(pw):
    """ผู้มาจอดไม่มีบัญชี ต้องเปิดได้ตรง ๆ ไม่ใช่ถูกเด้งไปหน้า login"""
    assert TestClient(app).get("/out", follow_redirects=False).status_code == 200


def test_password_never_reaches_the_browser(pw):
    c = TestClient(app)
    assert PW not in c.get("/out").text
    typed = "ที่พิมพ์ผิดไป"
    r = c.post("/out", data={**OUT, "entry_pw": typed})
    assert r.status_code == 200
    assert PW not in r.text and typed not in r.text
    assert "รหัสเจ้าหน้าที่ไม่ถูกต้อง" in r.text


@needs_db
def test_wrong_password_says_nothing_about_the_slip(pw, clean):
    """รหัสผิดต้องไม่เผยว่าทะเบียนนี้มีใบอยู่หรือไม่ และต้องไม่ปิดใบ

    ถ้าตอบ "ไม่พบใบของทะเบียนนี้" ให้คนที่พิมพ์รหัสผิด เท่ากับบอกไปแล้วครึ่งหนึ่งว่า
    ทะเบียนไหนจอดอยู่ที่นี่ — เป็นข้อมูลที่ใช้ตามรอยคนได้ จึงต้องเช็กรหัสก่อนค้นเสมอ
    """
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/out", data={**OUT, "entry_pw": "มั่ว"})
    assert "สมชาย" not in r.text
    assert "ไม่พบใบ" not in r.text
    assert _row()["car_status"] == "stored"


# ---------- เส้นทางปกติ ----------

@needs_db
def test_closes_the_slip_and_shows_a_receipt(pw, clean):
    """กรอกเบอร์+ทะเบียนตรง แล้วเจ้าหน้าที่ยืนยัน = ใบถูกปิดและได้ใบสรุปให้แคป"""
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/out", data=OUT)
    assert r.status_code == 200
    assert "นำรถออกแล้ว" in r.text
    assert "แคปหน้าจอ" in r.text

    row = _row()
    assert row["car_status"] == "returned"
    assert row["returned_at"] is not None
    assert row["returned_by"] == main.RETURNED_BY_OUT
    # released_to ต้องว่าง — ช่องนั้นหมายถึง "คนมารับที่ไม่ใช่เจ้าของ" ถ้าเติมชื่อบนใบลงไป
    # จะกลายเป็นการบันทึกว่าเราตรวจบัตรใครมาแล้ว ซึ่งเส้นทางนี้ไม่ได้ตรวจ
    assert row["released_to"] is None


@needs_db
@pytest.mark.parametrize("plate", ["1กก1234", "1กก 1234", " 1กก-1234 ", "1กก.1234",
                                  "1กก1234 ปทุมธานี"])
def test_plate_matches_regardless_of_spacing_and_province(pw, clean, plate):
    """ช่องว่าง ขีด จุด ชื่อจังหวัดต่อท้าย ต้องไม่ทำให้ค้นใบไม่เจอ

    ขาออกเทียบจาก plate_norm ที่ normalize ไว้แล้วตอนลงทะเบียน ไม่ใช่เทียบข้อความดิบ
    ถ้าเทียบดิบ คนที่พิมพ์เว้นวรรคไม่เหมือนขาเข้าจะถูกบล็อกทั้งที่เป็นเจ้าของรถจริง
    """
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/out", data={**OUT, "noplate": plate})
    assert "นำรถออกแล้ว" in r.text, plate


@needs_db
@pytest.mark.parametrize("tel", ["0812345678", "081-234-5678", "081 234 5678",
                                 "+66812345678"])
def test_phone_matches_regardless_of_formatting(pw, clean, tel):
    c = TestClient(app)
    c.post("/in", data=IN)
    assert "นำรถออกแล้ว" in c.post("/out", data={**OUT, "tel": tel}).text


@needs_db
def test_both_fields_must_match_not_just_one(pw, clean):
    """ทะเบียนถูกแต่เบอร์ผิด = ไม่ผ่าน และต้องไม่ปิดใบ (เงื่อนไขเป็น AND จริง)"""
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/out", data={**OUT, "tel": "0899999999"})
    assert "เบอร์โทรไม่ตรง" in r.text
    assert _row()["car_status"] == "stored"


@needs_db
def test_unknown_plate_and_already_returned_say_different_things(pw, clean):
    """"ไม่พบใบ" กับ "รับรถกลับไปแล้ว" ต้องเป็นข้อความคนละแบบ

    วิธีแก้คนละเรื่องกัน: อันแรกให้ตรวจตัวอักษรที่พิมพ์ อันหลังคือรถออกไปแล้วจริง
    ถ้าข้อความเหมือนกัน คนที่รถออกไปแล้วจะยืนกรอกซ้ำอยู่อย่างนั้นโดยไม่รู้ว่าเกิดอะไรขึ้น
    """
    c = TestClient(app)
    c.post("/in", data=IN)

    r = c.post("/out", data={**OUT, "noplate": "9ขข9999"})
    assert "ไม่พบใบจอดของทะเบียนนี้" in r.text

    assert "นำรถออกแล้ว" in c.post("/out", data=OUT).text
    r = c.post("/out", data=OUT)
    assert "รับรถกลับไปแล้วเมื่อ" in r.text
    assert "นำรถออกแล้ว" not in r.text


@needs_db
def test_slip_without_a_phone_on_file_passes_on_plate_alone(pw, clean):
    """ใบที่ OCR อ่านเบอร์ไม่ออก ต้องยังปล่อยรถได้ด้วยทะเบียน + รหัสเจ้าหน้าที่

    ถ้าบังคับให้เบอร์ตรงทุกใบ คนที่มารับรถจริงจะถูกบล็อกด้วยข้อมูลที่เราเองอ่านไม่ได้
    แล้วเจ้าหน้าที่จะปล่อยรถโดยไม่บันทึกอะไรเลย ซึ่งแย่กว่า
    """
    from ocrslip.db import connect
    c = TestClient(app)
    c.post("/in", data=IN)
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET tel = NULL, tel_digits = NULL")
        conn.commit()
    assert "นำรถออกแล้ว" in c.post("/out", data=OUT).text


@needs_db
def test_rejected_and_superseded_slips_are_not_pickable(pw, clean):
    """ใบที่ถูกตีกลับหรือตีว่าซ้ำ ไม่ใช่การฝากจริง ต้องไม่ถูกนับว่าเป็นใบที่รอรับรถ"""
    from ocrslip.db import connect
    c = TestClient(app)
    c.post("/in", data=IN)
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET review_status = 'rejected'")
        conn.commit()
    assert "ไม่พบใบจอดของทะเบียนนี้" in c.post("/out", data=OUT).text


# ---------- ทะเบียนเดียวมีหลายใบที่ยังจอดอยู่ ----------

@needs_db
def test_two_open_slips_ask_which_one_instead_of_guessing(pw, clean):
    """มีใบที่ยังจอดอยู่สองใบ = ต้องให้เลือก ไม่ใช่เดาปิดใบใดใบหนึ่ง

    ปิดผิดใบแล้วจะเหลือใบค้างที่ไม่มีใครมารับตลอดไป — เงียบและหาไม่เจอ
    """
    from ocrslip.db import connect
    c = TestClient(app)
    c.post("/in", data=IN)
    with connect() as conn:
        # ใบที่สองของทะเบียนเดียวกัน (คนละวันที่ฝาก) — เลียนใบซ้ำที่หลุดตัวจับซ้ำมาได้จริง
        conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slips
                (name, tel, tel_digits, plate_raw, plate_norm, deposit_date, location,
                 review_status, needs_review, car_status)
                VALUES ('สมชาย ใจดี', '0812345678', '0812345678', '1กก 1234', '1กก1234',
                        '2026-09-20', 'อาคาร 2 ชั้น 1', 'approved', false, 'stored')""")
        conn.commit()

    r = c.post("/out", data=OUT)
    assert "มีใบที่ยังจอดอยู่ 2 ใบ" in r.text
    assert PW not in r.text, "หน้าเลือกใบห้ามมีรหัสเจ้าหน้าที่ติดไปด้วย"
    # ห้ามติ๊กใบไหนไว้ล่วงหน้า — หน้านี้โผล่มาเพราะระบบเดาไม่ได้ว่าใบไหน
    # ถ้าติ๊กให้ก่อน คนที่รีบจะกดยืนยันโดยไม่ได้อ่าน ซึ่งคือการปิดผิดใบที่หน้านี้ตั้งใจจะกัน
    assert "checked" not in r.text
    # ใบของรอบล่าสุด (ฝาก 28/09) ต้องอยู่เหนือใบรอบเก่า (ฝาก 20/09) ในหน้า
    assert r.text.index("28/09/2026") < r.text.index("20/09/2026")
    with connect() as conn:
        n = conn.execute(
            f"SELECT count(*) AS n FROM {TEST_SCHEMA}.slips WHERE car_status = 'stored'"
        ).fetchone()["n"]
    assert n == 2, "ตอนถามว่าใบไหน ต้องยังไม่ปิดใบใด"


@needs_db
def test_picking_a_slip_id_from_another_car_is_rejected(pw, clean):
    """ยิง slip_id ของรถคันอื่นมาพร้อมทะเบียนตัวเอง ต้องปิดใบนั้นไม่ได้

    slip_id มาจากหน้าเลือกใบ จึงเป็นค่าที่ client ส่งมา ห้ามเชื่อตรง ๆ —
    ต้องอยู่ในผลค้นของทะเบียน+เบอร์ที่กรอกมาในคำขอเดียวกันเท่านั้น
    """
    from ocrslip.db import connect
    c = TestClient(app)
    c.post("/in", data=IN)
    victim = _row()["id"]
    other = {**IN, "tel": "0899999999", "noplate": "9ขข 9999", "name": "สมหญิง ใจงาม"}
    c.post("/in", data=other)

    r = c.post("/out", data={"tel": "0899999999", "noplate": "9ขข9999",
                             "slip_id": str(victim), "entry_pw": PW})
    # ของตัวเองมีใบเดียวจึงถูกปิดไปตามปกติ แต่ใบของคันที่ยิง id มาต้องไม่ถูกแตะ
    assert "นำรถออกแล้ว" in r.text
    with connect() as conn:
        row = conn.execute(
            f"SELECT car_status FROM {TEST_SCHEMA}.slips WHERE id = %s", (victim,)
        ).fetchone()
    assert row["car_status"] == "stored"


@needs_db
def test_second_press_does_not_overwrite_the_first_release(pw, clean):
    """กดยืนยันซ้ำ / มีเจ้าหน้าที่ปิดจากหน้าใบไปก่อน ต้องไม่ทับร่องรอยของคนแรก"""
    from ocrslip.db import connect, mark_returned
    c = TestClient(app)
    c.post("/in", data=IN)
    slip_id = str(_row()["id"])
    with connect() as conn:
        assert mark_returned(conn, slip_id, "เจ้าหน้าที่ ก", None) is True
        conn.commit()

    r = c.post("/out", data=OUT)
    assert "นำรถออกแล้ว" not in r.text
    assert _row()["returned_by"] == "เจ้าหน้าที่ ก"
