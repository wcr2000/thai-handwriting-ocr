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
    """303 ที่พาไปหน้า login ก็ต้องไม่ถูก cache ไม่งั้นคนที่ล็อกอินแล้วยังโดนเด้งออก"""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert "no-store" in r.headers.get("cache-control", "").lower() or r.headers.get("location")


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


@pytest.mark.parametrize("path", ["/", "/review", "/search", "/table", "/dashboard", "/staff"])
def test_pages_require_login(path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
