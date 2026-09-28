"""กันหน้าเว็บถูก cache จนผู้ใช้เห็นฟอร์มเวอร์ชันเก่าหลัง deploy

เคยเกิดขึ้นจริง: ฟอร์มเก่าที่ Safari cache ไว้ไม่มีช่องที่ server เวอร์ชันใหม่บังคับกรอก
ผู้ใช้จึงกดส่งแล้วขึ้น error ที่แก้ไม่ได้เลยเพราะไม่มีช่องให้กรอก
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip.web.main import app

client = TestClient(app)

# รหัสผ่านสำหรับเทสเท่านั้น — เทสตั้ง env ของบัญชีเองทุกครั้ง จะได้ไม่มีรหัสผ่านจริงอยู่ใน git
# และเทสไม่พังทุกครั้งที่เปลี่ยนรหัสผ่านของระบบจริง
TEST_PW = "pw-for-test"


@pytest.fixture
def accounts(monkeypatch):
    """ยัดบัญชีทดสอบครบทุก role ลง env แล้วคืนฟังก์ชันสำหรับล็อกอิน"""
    from ocrslip import auth

    h = auth.hash_password(TEST_PW)
    for prefix, name in (("ADMIN", "admin"), ("USER", "staff"), ("APPROVE", "approve")):
        monkeypatch.setenv(f"{prefix}_USERNAME", name)
        monkeypatch.setenv(f"{prefix}_PASSWORD_HASH", h)
    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()

    def login(username: str) -> TestClient:
        c = TestClient(app)
        r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303, f"ล็อกอิน {username} ไม่ผ่าน"
        return c

    return login


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


def test_forbidden_page_is_not_cached(accounts):
    """หน้า 403 ที่ auth_gate คืนเองก็ต้องมี header ด้วย ไม่งั้นค้างอยู่ในเครื่องแม้สิทธิ์เปลี่ยนแล้ว

    ใช้ client แยกตัว เพราะ TestClient เก็บ cookie ไว้ข้ามเทส
    ถ้าล็อกอินค้างไว้ เทสอื่นที่ตรวจ "ยังไม่ล็อกอินต้องโดนเด้ง" จะพังตามไปด้วย
    """
    staff_client = accounts("staff")
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


def test_image_keeps_its_own_cache_policy(accounts):
    """รูปหลักฐานไม่เคยเปลี่ยน ให้ cache ได้นาน — middleware ต้องไม่ไปทับ"""
    from ocrslip.config import DB_SCHEMA
    from ocrslip.db import connect

    # ต้องเจาะจงใบที่ "มีรูป" ไม่ใช่ใบล่าสุดเฉย ๆ — ใบที่ผู้มาจอดกรอกเอง (entry_source
    # typed) ไม่มีรูปหลักฐาน ถ้าใบล่าสุดเป็นแบบนั้น /image ตอบ 404 แล้วเทสต์ฟ้องผิดเรื่อง
    with connect() as conn:
        row = conn.execute(
            f"""SELECT s.id::text AS id FROM {DB_SCHEMA}.slips s
                 WHERE EXISTS (SELECT 1 FROM {DB_SCHEMA}.slip_images i WHERE i.slip_id = s.id)
                 ORDER BY s.created_at DESC LIMIT 1"""
        ).fetchone()
    if not row:
        pytest.skip("ยังไม่มีใบที่มีรูปหลักฐานใน DB")
    r = accounts("admin").get(f"/image/{row['id']}")
    assert "max-age" in r.headers.get("cache-control", "")
    assert "no-store" not in r.headers.get("cache-control", "")


@pytest.mark.parametrize("path", ["/", "/review", "/search", "/table", "/dashboard", "/staff"])
def test_pages_require_login(path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
