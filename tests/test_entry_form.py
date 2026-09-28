"""ฟอร์มขาเข้าที่ผู้มาจอดกรอกเอง (/in)

หน้านี้เปิดให้คนนอกเข้าได้ ด่านเดียวของมันคือรหัสที่เจ้าหน้าที่พิมพ์ปิดท้าย
เทสต์ที่สำคัญที่สุดในไฟล์นี้จึงเป็น "รหัสต้องไม่โผล่ออกไปกับหน้าเว็บ" —
ถ้าหลุดไปอยู่ใน HTML ที่ส่งให้เครื่องของผู้มาจอด ใครก็สร้างใบจอดปลอมได้จากที่บ้าน
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip.web import main
from ocrslip.web.main import app

from conftest import TEST_SCHEMA, needs_db

PW = "รหัสทดสอบ-xyz"

GOOD = {
    "name": "สมชาย ใจดี", "tel": "081-234-5678", "date": "2026-09-28",
    "noplate": "1กก 1234", "province": "ปทุมธานี", "brand": "โตโยต้า",
    "typecar": "เก๋ง", "building": "อาคาร 1", "floor": "ชั้น 2",
    "entry_pw": PW,
}


@pytest.fixture
def pw(pgenv, monkeypatch):
    """ตั้งรหัส + ชี้ฐานข้อมูลไปที่ฐานทดสอบ

    ต้องพ่วง pgenv ด้วยตั้งแต่ /in เริ่มอ่านตัวเลือกอาคาร/ชั้นจากตาราง app_settings —
    ก่อนหน้านี้เทสต์กลุ่มนี้ไม่แตะฐานข้อมูลเลยจึงไม่ต้องใช้ แต่ตอนนี้ถ้าไม่พ่วง
    มันจะไปเปิด connection ตาม DATABASE_URL ใน .env ซึ่งชี้ production
    """
    monkeypatch.setattr(main, "ENTRY_PASSWORD", PW)


def test_page_is_off_when_no_password_configured(monkeypatch):
    """ไม่ได้ตั้งรหัส = ปิดหน้าไปเลย ดีกว่าเปิดรับข้อมูลโดยไม่มีด่านอะไรเลย"""
    monkeypatch.setattr(main, "ENTRY_PASSWORD", "")
    assert TestClient(app).get("/in").status_code == 503
    assert TestClient(app).post("/in", data=GOOD).status_code == 503


def test_open_to_public_without_login(pw):
    """ผู้มาจอดไม่มีบัญชี ต้องเปิดได้ตรง ๆ ไม่ใช่ถูกเด้งไปหน้า login"""
    r = TestClient(app).get("/in", follow_redirects=False)
    assert r.status_code == 200


def test_password_never_reaches_the_browser(pw):
    """รหัสห้ามอยู่ใน HTML ทั้งตอนเปิดหน้าและตอนตอบกลับว่ากรอกผิด

    นี่คือเหตุผลที่การตรวจรหัสต้องอยู่ฝั่ง server เท่านั้น ถ้าย้ายไปเช็คใน JavaScript
    เทสต์นี้จะพังทันที — ซึ่งเป็นสิ่งที่ต้องการ
    """
    c = TestClient(app)
    assert PW not in c.get("/in").text
    # กรอกรหัสผิด: หน้าที่ตอบกลับต้องไม่มีทั้งรหัสจริงและรหัสที่เพิ่งพิมพ์ไป
    typed = "ที่พิมพ์ผิดไป"
    r = c.post("/in", data={**GOOD, "entry_pw": typed})
    assert r.status_code == 200
    assert PW not in r.text
    assert typed not in r.text
    assert "รหัสเจ้าหน้าที่ไม่ถูกต้อง" in r.text


@pytest.mark.parametrize("bad,field,msg", [
    ({"tel": "0812345"}, "tel", "10 หลัก"),
    ({"name": "สมชาย"}, "name", "ชื่อและนามสกุล"),
    ({"noplate": ""}, "noplate", "กรอกทะเบียน"),
    ({"province": "ปทุม"}, "province", "เลือกจังหวัด"),
    ({"building": "ตึกไหนก็ได้"}, "building", "เลือกอาคาร"),
    ({"floor": "ชั้นลอย"}, "floor", "เลือกชั้น"),
])
def test_rejects_bad_input_and_keeps_what_was_typed(pw, bad, field, msg):
    """ตีกลับแล้วต้องคืนค่าที่กรอกไว้ให้ด้วย ไม่ใช่ล้างฟอร์มให้พิมพ์ใหม่ทั้งหน้าบนมือถือ"""
    r = TestClient(app).post("/in", data={**GOOD, **bad})
    assert r.status_code == 200
    assert msg in r.text
    assert "สมชาย" in r.text or field == "name"


def test_province_and_building_must_come_from_the_list(pw):
    """ค่าที่ไม่อยู่ใน dropdown ต้องถูกปฏิเสธ ไม่ใช่เชื่อเพราะ <select> ไม่มีให้เลือก

    <select> กันได้แค่คนที่ใช้หน้าเว็บตามปกติ ใครยิง POST ตรงก็ส่งอะไรมาก็ได้
    ถ้าไม่ตรวจซ้ำฝั่ง server ที่จอดจะกลายเป็นข้อความอะไรก็ได้ แล้วสรุปยอดไม่ได้
    """
    r = TestClient(app).post("/in", data={**GOOD, "province": "<script>"})
    assert "เลือกจังหวัด" in r.text


# ---------- ส่วนที่ต้องใช้ Postgres จริง ----------

@pytest.fixture
def clean(pgenv):
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.commit()


@needs_db
def test_accepted_entry_is_searchable_immediately(pw, clean):
    """กรอกเองแล้วเจ้าหน้าที่ยืนยัน = ไม่มีอะไรให้ตรวจ ต้องค้นเจอทันที

    ถ้าใบพวกนี้ยังเข้าคิวตรวจ คอขวด labeling จะไม่หายไปไหน และขาออกจะหาใบไม่เจอ
    เพราะหน้าค้นหาแสดงเฉพาะใบที่ approved
    """
    r = TestClient(app).post("/in", data=GOOD)
    assert r.status_code == 200
    assert "เจ้าหน้าที่ยืนยันแล้ว" in r.text

    from ocrslip.db import connect
    with connect() as conn:
        row = conn.execute(f"SELECT * FROM {TEST_SCHEMA}.slips").fetchone()
    assert row["review_status"] == "approved"
    assert row["needs_review"] is False
    assert row["entry_source"] == "typed"
    assert row["car_status"] == "stored"
    # ที่จอดต้องประกอบจาก dropdown ทั้งสองช่อง ไม่ใช่เก็บแค่ช่องเดียว
    assert row["location"] == "อาคาร 1 ชั้น 2"
    assert row["tel_digits"] == "0812345678"
    # ไม่มีค่าใช้จ่าย AI ในเส้นทางนี้ — ถ้าเลขนี้ขึ้นแปลว่ามีใครพา OCR กลับมา
    assert row["ocr_cost_usd"] == 0


@needs_db
def test_submitting_twice_does_not_create_a_second_slip(pw, clean):
    """กด submit ซ้ำ / refresh หน้า ต้องได้ใบเดิม ไม่ใช่รถคันเดียวมีสองใบ

    ใบซ้ำเจ็บที่ขาออก: เจ้าหน้าที่เห็นสองแถวเหมือนกันแล้วไม่รู้ว่าต้องปิดใบไหน
    ปิดผิดใบก็เหลือใบค้างที่ไม่มีใครมารับตลอดไป
    """
    c = TestClient(app)
    c.post("/in", data=GOOD)
    r = c.post("/in", data={**GOOD, "brand": "ฮอนด้า"})
    assert "ลงทะเบียนไว้แล้ว" in r.text

    from ocrslip.db import connect
    with connect() as conn:
        n = conn.execute(f"SELECT count(*) AS n FROM {TEST_SCHEMA}.slips").fetchone()["n"]
    assert n == 1


@needs_db
def test_same_plate_can_register_again_after_it_left(pw, clean):
    """รถคันเดิมกลับมาจอดใหม่หลังรับรถกลับไปแล้ว ต้องลงทะเบียนได้ ไม่ใช่ติดกันซ้ำ"""
    from ocrslip.db import connect, mark_returned
    c = TestClient(app)
    c.post("/in", data=GOOD)
    with connect() as conn:
        slip_id = conn.execute(f"SELECT id::text AS id FROM {TEST_SCHEMA}.slips").fetchone()["id"]
        assert mark_returned(conn, slip_id, "เจ้าหน้าที่ ก", None) is True
        conn.commit()

    c.post("/in", data=GOOD)
    with connect() as conn:
        n = conn.execute(f"SELECT count(*) AS n FROM {TEST_SCHEMA}.slips").fetchone()["n"]
    assert n == 2


@needs_db
def test_second_release_does_not_overwrite_the_first(pw, clean):
    """สองเจ้าหน้าที่กดปล่อยรถใบเดียวกัน คนที่กดทีหลังต้องไม่ทับร่องรอยของคนแรก

    ที่จุด checkout มีหลายเครื่องหันจอคนละทาง เรื่องนี้เกิดขึ้นจริง
    ถ้าทับได้ ชื่อผู้ส่งมอบกับเวลาจะกลายเป็นของคนที่กดทีหลัง = ลบร่องรอยคนที่ปล่อยรถจริง
    """
    from ocrslip.db import connect, mark_returned
    TestClient(app).post("/in", data=GOOD)
    with connect() as conn:
        slip_id = conn.execute(f"SELECT id::text AS id FROM {TEST_SCHEMA}.slips").fetchone()["id"]
        assert mark_returned(conn, slip_id, "คนแรก", None, "ญาติ ก") is True
        assert mark_returned(conn, slip_id, "คนที่สอง", None, "ญาติ ข") is False
        conn.commit()
        row = conn.execute(
            f"SELECT returned_by, released_to FROM {TEST_SCHEMA}.slips WHERE id = %s", (slip_id,)
        ).fetchone()
    assert row["returned_by"] == "คนแรก"
    assert row["released_to"] == "ญาติ ก"


# ---------- มือถือ ----------

def test_mobile_touch_rule_covers_every_input_type_we_use():
    """ชนิด input ทุกตัวที่ฟอร์มใช้จริง ต้องอยู่ในกฎ 44px ของ media query มือถือ

    กฎนี้เคยตก input[type=tel] กับ input[type=date] ไป — ซึ่งดันเป็นช่องเบอร์โทร
    กับช่องวันที่ในฟอร์มขาเข้าพอดี ช่องสองช่องนั้นจึงเตี้ยกว่า 44px บนมือถือ
    บั๊กแบบนี้ไม่มีใครเห็นบนเดสก์ท็อป และคนกรอกจริงคือคนที่ยืนอยู่ข้างรถใช้นิ้วโป้งข้างเดียว
    """
    import re
    from pathlib import Path

    css = Path("ocrslip/web/static/app.css").read_text(encoding="utf-8")
    forms = " ".join(
        Path(f"ocrslip/web/templates/{n}").read_text(encoding="utf-8")
        for n in ("in.html", "slip.html", "index.html")
    )
    used = set(re.findall(r'<input[^>]*type="(\w+)"', forms)) - {"hidden", "checkbox", "file"}

    block = css.split("@media (max-width: 820px)", 1)[1].split("}", 1)[0]
    covered = set(re.findall(r"input\[type=(\w+)\]", block))
    assert used <= covered, f"media query มือถือยังไม่ครอบ: {sorted(used - covered)}"
