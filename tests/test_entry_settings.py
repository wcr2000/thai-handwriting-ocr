"""ตั้งค่าตัวเลือกอาคาร/ชั้นของฟอร์มขาเข้า จากหน้าเว็บ

เหตุผลที่ย้ายมาไว้บนหน้าเว็บคือ "งานแต่ละครั้งเปิดอาคารไม่เหมือนกัน และคนที่รู้ว่า
วันนี้เปิดอาคารไหนคือคนหน้างาน ไม่ใช่คนที่ถือ dashboard ของ Render" เทสต์ในไฟล์นี้
จึงเน้นสองเรื่อง: แก้แล้วมีผลทันทีโดยไม่ต้องรีสตาร์ต และสิทธิ์ต้องเป็นของ admin เท่านั้น
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from ocrslip import auth
from ocrslip.daystamp import day_stamp
from ocrslip.web import main
from ocrslip.web.main import app

from conftest import TEST_PW, TEST_SCHEMA, needs_db

pytestmark = needs_db

ENTRY_PW = "รหัสทดสอบ-xyz"

GOOD = {
    "name": "สมชาย ใจดี", "tel": "0812345678", "date": "2026-09-28",
    "noplate": "1กก 1234", "province": "ปทุมธานี", "brand": "โตโยต้า",
    "typecar": "เก๋ง", "entry_pw": ENTRY_PW,
}


@pytest.fixture
def admin(pgenv, monkeypatch):
    """ล้างค่าตั้งเดิมทุกครั้ง แล้วคืน client ที่ล็อกอินเป็น admin"""
    from ocrslip.db import connect

    monkeypatch.setattr(main, "ENTRY_PASSWORD", ENTRY_PW)
    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.app_settings")
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.commit()

    c = TestClient(app)
    r = c.post("/login", data={"username": "admin", "password": TEST_PW, "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303
    return c


def _login(username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.mark.parametrize("username", ["staff", "approve"])
def test_only_admin_can_open_or_save_settings(admin, username):
    """หน้านี้เปลี่ยนสิ่งที่คนนอกเห็นในฟอร์มสาธารณะ จึงต้องเป็นของ admin เท่านั้น"""
    c = _login(username)
    assert c.get("/settings", follow_redirects=False).status_code == 403
    assert c.post("/settings", data={"buildings": "x", "floors": "y"}).status_code == 403


def test_saved_buildings_show_up_in_the_public_form_right_away(admin):
    """แก้แล้วต้องมีผลทันที — ถ้าต้องรีสตาร์ตถึงจะเห็น ก็ไม่ต่างจากการตั้งใน env"""
    admin.post("/settings", data={"buildings": "อาคารบุญ\nอาคารธรรม", "floors": "ชั้น 1\nชั้น 2"})

    html = TestClient(app).get("/in").text
    assert "อาคารบุญ" in html and "อาคารธรรม" in html
    # ค่าตั้งต้นจาก env ต้องหายไป ไม่ใช่ต่อท้ายกันจนมีทั้งสองชุด
    assert "ลานจอดรอบนอก" not in html


def test_clearing_the_boxes_falls_back_to_env_defaults(admin):
    """ล้างช่อง = ถอยกลับไปใช้ค่าใน .env ไม่ใช่ตั้งรายการเป็น "ว่าง" ทับค่าตั้งต้น

    ถ้าเก็บสตริงว่างไว้แทนการลบแถว ฟอร์มขาเข้าจะเหลือ dropdown ที่ไม่มีตัวเลือกอะไรเลย
    แล้วไม่มีใครลงทะเบียนได้ทั้งงาน
    """
    admin.post("/settings", data={"buildings": "อาคารเดียว", "floors": "ชั้นเดียว"})
    admin.post("/settings", data={"buildings": "   \n  ", "floors": ""})

    html = TestClient(app).get("/in").text
    assert "อาคารเดียว" not in html
    assert main.ENTRY_BUILDINGS[0] in html


def test_public_form_rejects_a_building_that_is_no_longer_offered(admin):
    """ตรวจฝั่ง server กับรายการ "ปัจจุบัน" เสมอ ไม่ใช่รายการตอนที่หน้าถูก render

    คนเปิดหน้าค้างไว้ตั้งแต่เช้า แล้วผู้ดูแลปิดอาคารนั้นไปตอนบ่าย ค่าที่ส่งมาทีหลัง
    ต้องไม่ผ่าน ไม่งั้นได้ใบที่ระบุที่จอดซึ่งวันนั้นไม่ได้เปิดใช้
    """
    admin.post("/settings", data={"buildings": "อาคารบุญ", "floors": "ชั้น 1"})

    c = TestClient(app)
    r = c.post("/in", data={**GOOD, "building": "อาคาร 1", "floor": "ชั้น 1"})
    assert "เลือกอาคาร" in r.text

    r = c.post("/in", data={**GOOD, "building": "อาคารบุญ", "floor": "ชั้น 1"})
    assert "เจ้าหน้าที่ยืนยันแล้ว" in r.text


def test_settings_page_never_offers_to_edit_the_entry_password(admin):
    """รหัสเจ้าหน้าที่ต้องอยู่ใน .env เท่านั้น — แก้จากหน้าเว็บได้แปลว่าต้องเก็บ
    แบบอ่านกลับได้ในฐานข้อมูล แล้วความลับจะติดไปกับ backup ทุกชุด
    """
    html = admin.get("/settings").text
    assert ENTRY_PW not in html
    assert 'name="entry_pw"' not in html
    assert 'name="entry_password"' not in html


def test_saved_quotes_show_up_on_the_slip_right_away(admin):
    """คำคมที่ทีมตั้งเอง ต้องขึ้นบนใบที่ผู้มาจอดแคปเก็บไว้ทันที

    28/09/2026 เป็นวันจันทร์ ใส่คำคมบรรทัดเดียวเพื่อให้รู้แน่ว่าจะได้บรรทัดไหน
    ไม่ต้องคำนวณว่าวันนั้นวนไปถึงคำคมอันที่เท่าไหร่
    """
    admin.post("/settings", data={"buildings": "อาคารบุญ", "floors": "ชั้น 1",
                                  "quotes": "คำคมของทีมเราเอง"})

    r = TestClient(app).post("/in", data={**GOOD, "building": "อาคารบุญ", "floor": "ชั้น 1"})
    assert "คำคมของทีมเราเอง" in r.text
    assert "วันจันทร์" in r.text


def test_clearing_the_quotes_falls_back_to_the_built_in_set(admin):
    """ล้างคำคมจนหมดต้องกลับไปใช้ชุดที่มากับระบบ ไม่ใช่ได้แถบเปล่า ๆ หรือหน้าพัง

    หน้านี้คือหน้าสุดท้ายของการลงทะเบียน ถ้ามันพังคือรถเข้ามาจอดแล้วแต่ไม่มีใบให้แคป
    """
    admin.post("/settings", data={"buildings": "อาคารบุญ", "floors": "ชั้น 1",
                                  "quotes": "คำคมของทีมเราเอง"})
    admin.post("/settings", data={"buildings": "อาคารบุญ", "floors": "ชั้น 1", "quotes": "  \n "})

    r = TestClient(app).post("/in", data={**GOOD, "building": "อาคารบุญ", "floor": "ชั้น 1"})
    assert "คำคมของทีมเราเอง" not in r.text
    assert day_stamp(dt.date(2026, 9, 28))["quote"] in r.text
