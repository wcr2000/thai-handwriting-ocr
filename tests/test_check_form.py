"""The car-check form (/check): the owner visits a car that stays parked.

Same gate as /out (plate + phone + staff passcode), but it must never close the slip — the car is
still here. What it does is leave one row in car_checks saying who came, when, and why.
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip.web import main
from ocrslip.web.main import app

from conftest import TEST_SCHEMA, needs_db

PW = "รหัสทดสอบ-xyz"

IN = {
    "name": "สมชาย ใจดี", "tel": "081-234-5678", "date": "2026-09-28",
    "noplate": "1กก 1234", "province": "ปทุมธานี", "brand": "โตโยต้า",
    "typecar": "เก๋ง", "building": "อาคาร 1", "floor": "ชั้น 2",
    "entry_pw": PW,
}
CHECK = {"tel": "0812345678", "noplate": "1กก1234", "reason": "มาเอาของในรถ", "entry_pw": PW}


@pytest.fixture
def pw(pgenv, monkeypatch):
    monkeypatch.setattr(main, "ENTRY_PASSWORD", PW)


@pytest.fixture
def clean(pgenv):
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.app_settings")
        conn.commit()


def _q(sql):
    from ocrslip.db import connect
    with connect() as conn:
        return conn.execute(sql).fetchall()


def test_page_is_off_when_no_password_configured(monkeypatch):
    monkeypatch.setattr(main, "ENTRY_PASSWORD", "")
    assert TestClient(app).get("/check").status_code == 503
    assert TestClient(app).post("/check", data=CHECK).status_code == 503


@needs_db
def test_open_to_public_and_offers_default_reasons_plus_other(pw, clean):
    r = TestClient(app).get("/check", follow_redirects=False)
    assert r.status_code == 200
    for reason in main.CHECK_REASONS:
        assert reason in r.text
    assert "อื่นๆ" in r.text
    assert PW not in r.text


@needs_db
def test_logs_the_visit_and_leaves_the_slip_open(pw, clean):
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/check", data=CHECK)
    assert "ให้เข้าเช็ครถได้" in r.text
    assert PW not in r.text

    assert _q(f"SELECT car_status FROM {TEST_SCHEMA}.slips")[0]["car_status"] == "stored"
    checks = _q(f"SELECT * FROM {TEST_SCHEMA}.car_checks")
    assert len(checks) == 1
    assert checks[0]["reason"] == "มาเอาของในรถ"
    assert checks[0]["checked_by"] == main.CHECKED_BY_FORM


@needs_db
def test_other_reason_takes_the_typed_text(pw, clean):
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/check", data={**CHECK, "reason": main.CHECK_OTHER, "reason_other": "มาเติมลมยาง"})
    assert "มาเติมลมยาง" in r.text
    assert _q(f"SELECT reason FROM {TEST_SCHEMA}.car_checks")[0]["reason"] == "มาเติมลมยาง"


@needs_db
def test_other_without_text_and_unknown_reason_are_rejected(pw, clean):
    c = TestClient(app)
    c.post("/in", data=IN)
    assert "พิมพ์เหตุผลที่มา" in c.post("/check", data={**CHECK, "reason": main.CHECK_OTHER}).text
    assert "เลือกเหตุผลที่มา" in c.post("/check", data={**CHECK, "reason": "แต่งเอง"}).text
    assert _q(f"SELECT * FROM {TEST_SCHEMA}.car_checks") == []


@needs_db
def test_wrong_password_says_nothing_about_the_slip(pw, clean):
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/check", data={**CHECK, "entry_pw": "มั่ว"})
    assert "รหัสเจ้าหน้าที่ไม่ถูกต้อง" in r.text
    assert "สมชาย" not in r.text and "ไม่พบใบ" not in r.text
    assert _q(f"SELECT * FROM {TEST_SCHEMA}.car_checks") == []


@needs_db
def test_wrong_phone_unknown_plate_and_returned_car_are_refused(pw, clean):
    c = TestClient(app)
    c.post("/in", data=IN)
    assert "เบอร์โทรไม่ตรง" in c.post("/check", data={**CHECK, "tel": "0899999999"}).text
    assert "ไม่พบใบจอด" in c.post("/check", data={**CHECK, "noplate": "9ขข9999"}).text
    c.post("/out", data={k: CHECK[k] for k in ("tel", "noplate", "entry_pw")})
    assert "รับรถกลับไปแล้ว" in c.post("/check", data=CHECK).text
    assert _q(f"SELECT * FROM {TEST_SCHEMA}.car_checks") == []


@needs_db
def test_reasons_come_from_settings(pw, clean):
    from ocrslip.db import connect, set_setting
    with connect() as conn:
        set_setting(conn, main.SETTING_CHECK_REASONS, "มาชาร์จแบต\nมาล้างรถ")
        conn.commit()
    c = TestClient(app)
    page = c.get("/check").text
    assert "มาชาร์จแบต" in page and "มาเอาของในรถ" not in page
    c.post("/in", data=IN)
    assert "ให้เข้าเช็ครถได้" in c.post("/check", data={**CHECK, "reason": "มาชาร์จแบต"}).text


# ---------- the way back (/check/out) ----------

OUT = {"tel": "0812345678", "noplate": "1กก1234", "entry_pw": PW}


@needs_db
def test_check_out_closes_the_visit_but_not_the_slip(pw, clean):
    c = TestClient(app)
    c.post("/in", data=IN)
    c.post("/check", data=CHECK)
    r = c.post("/check/out", data=OUT)
    assert "เช็ครถเสร็จแล้ว" in r.text
    assert PW not in r.text
    check = _q(f"SELECT * FROM {TEST_SCHEMA}.car_checks")[0]
    assert check["finished_at"] is not None
    assert _q(f"SELECT car_status FROM {TEST_SCHEMA}.slips")[0]["car_status"] == "stored"


@needs_db
def test_check_out_without_an_open_visit_is_refused(pw, clean):
    c = TestClient(app)
    c.post("/in", data=IN)
    assert "ไม่มีการเข้าเช็ครถที่ค้างอยู่" in c.post("/check/out", data=OUT).text
    c.post("/check", data=CHECK)
    c.post("/check/out", data=OUT)
    assert "ไม่มีการเข้าเช็ครถที่ค้างอยู่" in c.post("/check/out", data=OUT).text


@needs_db
def test_check_out_wrong_password_or_phone_closes_nothing(pw, clean):
    c = TestClient(app)
    c.post("/in", data=IN)
    c.post("/check", data=CHECK)
    r = c.post("/check/out", data={**OUT, "entry_pw": "มั่ว"})
    assert "รหัสเจ้าหน้าที่ไม่ถูกต้อง" in r.text and "สมชาย" not in r.text
    assert "เบอร์โทรไม่ตรง" in c.post("/check/out", data={**OUT, "tel": "0899999999"}).text
    assert _q(f"SELECT finished_at FROM {TEST_SCHEMA}.car_checks")[0]["finished_at"] is None


@needs_db
def test_second_check_in_while_one_is_open_is_refused(pw, clean):
    c = TestClient(app)
    c.post("/in", data=IN)
    c.post("/check", data=CHECK)
    assert "ยังไม่ได้แจ้งออก" in c.post("/check", data=CHECK).text
    assert len(_q(f"SELECT * FROM {TEST_SCHEMA}.car_checks")) == 1
    c.post("/check/out", data=OUT)
    assert "ให้เข้าเช็ครถได้" in c.post("/check", data=CHECK).text


@needs_db
def test_taking_the_car_out_closes_an_open_visit(pw, clean):
    """Someone who went in to check, then decided to drive off, never comes back via /check/out"""
    c = TestClient(app)
    c.post("/in", data=IN)
    c.post("/check", data=CHECK)
    assert "นำรถออกแล้ว" in c.post("/out", data=OUT).text
    assert _q(f"SELECT finished_at FROM {TEST_SCHEMA}.car_checks")[0]["finished_at"] is not None
