"""ลบใบถาวร — ปุ่มเดียวในระบบที่ย้อนกลับไม่ได้

มีไว้สำหรับใบทดสอบ/ใบกรอกมั่ว ซึ่งเก็บไว้มีแต่ทำให้ตัวเลขสรุปเพี้ยน
สิ่งที่ต้องกันให้แน่นคือ "ใครลบได้" เพราะพลาดแล้วไม่มีทางกู้
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip import auth
from ocrslip.web.main import app

from conftest import TEST_PW, TEST_SCHEMA, needs_db

pytestmark = needs_db


def _login(username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.fixture
def slip(pgenv):
    """ใบหนึ่งใบพร้อมรูปหลักฐานและประวัติการแก้ไข ไว้ดูว่าของพ่วงหายตามไปจริง"""
    from ocrslip.db import connect

    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        sid = conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slips (name, tel, plate_raw, plate_norm)
                VALUES ('ทดสอบ ลบ', '0800000000', '9กก 9999', '9กก9999')
                RETURNING id::text""").fetchone()["id"]
        conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slip_images (slip_id, kind, sha256, bytes)
                VALUES (%s, 'processed', 'deadbeef', %s)""", (sid, b"x"))
        conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slip_edits (slip_id, field, new_value)
                VALUES (%s, 'name', 'ทดสอบ ลบ')""", (sid,))
        conn.commit()
    return sid


def _counts(sid: str) -> tuple[int, int, int]:
    from ocrslip.db import connect
    with connect() as conn:
        one = lambda sql: conn.execute(sql, (sid,)).fetchone()["n"]  # noqa: E731
        return (one(f"SELECT count(*) n FROM {TEST_SCHEMA}.slips WHERE id = %s"),
                one(f"SELECT count(*) n FROM {TEST_SCHEMA}.slip_images WHERE slip_id = %s"),
                one(f"SELECT count(*) n FROM {TEST_SCHEMA}.slip_edits WHERE slip_id = %s"))


@pytest.mark.parametrize("username", ["staff", "approve"])
def test_only_admin_can_delete(slip, username):
    """staff ปล่อยรถได้ แต่ลบใบไม่ได้ — ปล่อยรถผิดยังตามแก้ได้ ลบผิดไม่มีทางกู้"""
    assert _login(username).post(f"/slips/{slip}/delete").status_code == 403
    assert _counts(slip)[0] == 1, "ใบต้องยังอยู่"


def test_admin_delete_removes_the_slip_and_everything_attached(slip):
    """ลบใบแล้วรูปหลักฐานกับประวัติการแก้ไขต้องหายตามไปด้วย ไม่ใช่เหลือซากที่ไม่มีเจ้าของ"""
    assert _counts(slip) == (1, 1, 1)
    r = _login("admin").post(f"/slips/{slip}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/search?deleted=")
    assert _counts(slip) == (0, 0, 0)


def test_deleting_the_same_slip_twice_is_not_a_crash(slip):
    """กด back แล้วกดซ้ำ ต้องได้ 404 ที่อ่านรู้เรื่อง ไม่ใช่ 500"""
    c = _login("admin")
    c.post(f"/slips/{slip}/delete", follow_redirects=False)
    r = c.post(f"/slips/{slip}/delete", follow_redirects=False)
    assert r.status_code == 404


def test_delete_box_is_hidden_from_non_admin(slip):
    """staff ต้องไม่เห็นแม้แต่กล่องลบ — ปุ่มที่กดไม่ได้แต่มองเห็นคือปุ่มที่ชวนให้ลอง"""
    assert "ลบใบนี้ถาวร" not in _login("staff").get(f"/slips/{slip}").text
    assert "ลบใบนี้ถาวร" in _login("admin").get(f"/slips/{slip}").text
