"""กันหน้าเว็บถูก cache จนผู้ใช้เห็นฟอร์มเวอร์ชันเก่าหลัง deploy

เคยเกิดขึ้นจริง: ฟอร์มเก่าที่ Safari cache ไว้ไม่มีช่องที่ server เวอร์ชันใหม่บังคับกรอก
ผู้ใช้จึงกดส่งแล้วขึ้น error ที่แก้ไม่ได้เลยเพราะไม่มีช่องให้กรอก
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip.web.main import app

client = TestClient(app)


def test_html_is_never_cached():
    r = client.get("/login")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")


def test_redirect_to_login_is_not_cached():
    """303 ที่พาไปหน้า login ก็ต้องไม่ถูก cache ไม่งั้นคนที่ล็อกอินแล้วยังโดนเด้งออก

    ห้ามเขียนเป็น `assert A or r.headers.get("location")` เพราะ location มีค่าเสมอ
    เทสจะเขียวตลอดโดยไม่ได้ตรวจอะไรเลย
    """
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    assert "no-store" in r.headers.get("cache-control", "").lower()


def test_forbidden_page_is_not_cached():
    """หน้า 403 ที่ auth_gate คืนเองก็ต้องมี header ด้วย ไม่งั้นค้างอยู่ในเครื่องแม้สิทธิ์เปลี่ยนแล้ว

    ใช้ client แยกตัว เพราะ TestClient เก็บ cookie ไว้ข้ามเทส
    ถ้าล็อกอินค้างไว้ เทสอื่นที่ตรวจ "ยังไม่ล็อกอินต้องโดนเด้ง" จะพังตามไปด้วย
    """
    with TestClient(app) as staff_client:
        staff_client.post(
            "/login", data={"username": "staff", "password": "flood2026-staff", "next": "/"}
        )
        r = staff_client.get("/table", follow_redirects=False)
    assert r.status_code == 403
    assert "no-store" in r.headers.get("cache-control", "").lower()


def test_static_must_revalidate():
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
    assert r.headers.get("etag"), "ต้องมี ETag ไม่งั้นต้องโหลดไฟล์ใหม่ทุกครั้ง"


def test_static_still_answers_304_with_etag():
    """no-cache = ถามก่อนใช้ ไม่ใช่ห้ามเก็บ — ต้องยังตอบ 304 ได้เพื่อไม่เปลืองเน็ตบนมือถือ"""
    etag = client.get("/static/app.css").headers["etag"]
    r = client.get("/static/app.css", headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert not r.content


def test_image_keeps_its_own_cache_policy():
    """รูปหลักฐานไม่เคยเปลี่ยน ให้ cache ได้นาน — middleware ต้องไม่ไปทับ"""
    from ocrslip.db import connect, list_slips

    with connect() as conn:
        slips = list_slips(conn, limit=1)
    if not slips:
        pytest.skip("ยังไม่มีข้อมูลใน DB")
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "flood2026-admin", "next": "/"})
        r = c.get(f"/image/{slips[0]['id']}")
    assert "max-age" in r.headers.get("cache-control", "")
    assert "no-store" not in r.headers.get("cache-control", "")


@pytest.mark.parametrize("path", ["/", "/review", "/search", "/table", "/dashboard", "/staff"])
def test_pages_require_login(path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
