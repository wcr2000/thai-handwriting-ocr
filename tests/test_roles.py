"""สิทธิ์ของแต่ละ role — พังแล้วคนนอกเห็นข้อมูลส่วนตัวของเจ้าของรถทั้งก้อน

จุดที่ต้องกันให้แน่นที่สุดคือ role "approver" (อาสาสมัครที่มาช่วยทำ label):
เขาต้องทำได้แค่ "อัปโหลด" กับ "อนุมัติใบในคิว" เท่านั้น
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip import auth
from ocrslip.web.main import app

TEST_PW = "pw-for-test"


@pytest.fixture
def login(monkeypatch):
    h = auth.hash_password(TEST_PW)
    for prefix, name in (("ADMIN", "admin"), ("USER", "staff"), ("APPROVE", "approve")):
        monkeypatch.setenv(f"{prefix}_USERNAME", name)
        monkeypatch.setenv(f"{prefix}_PASSWORD_HASH", h)
    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()

    def _login(username: str) -> TestClient:
        c = TestClient(app)
        r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303, f"ล็อกอิน {username} ไม่ผ่าน"
        return c

    return _login


def test_approve_account_gets_approver_role(login):
    user, err = auth.authenticate("approve", TEST_PW, "10.0.0.9")
    assert err == ""
    assert user.is_approver and not user.is_admin


def test_unknown_role_in_token_is_not_treated_as_staff():
    """token ที่ไม่มี role (ของเก่า) ต้องตกไปที่สิทธิ์น้อยที่สุด ไม่ใช่สิทธิ์เจ้าหน้าที่"""
    import json
    payload = json.dumps({"u": "x", "exp": 2**31}).encode()
    tok = auth._sign(payload, "k")
    assert auth.read_token(tok, "k").is_approver


@pytest.mark.parametrize("path", [
    "/search", "/api/search", "/table", "/dashboard", "/export.xlsx", "/staff",
    "/slips/00000000-0000-0000-0000-000000000000",
])
def test_approver_cannot_open_other_pages(login, path):
    assert login("approve").get(path, follow_redirects=False).status_code == 403


@pytest.mark.parametrize("path", ["/", "/review"])
def test_approver_can_open_its_own_pages(login, path):
    assert login("approve").get(path).status_code == 200


def test_approver_cannot_reject_or_return(login):
    c = login("approve")
    slip_id = "00000000-0000-0000-0000-000000000000"
    assert c.post(f"/review/{slip_id}/reject", data={"reason": "x"}).status_code == 403
    assert c.post(f"/slips/{slip_id}/return", data={}).status_code == 403


def test_approver_queue_falls_back_to_pending_pile(login):
    """ขอดูกอง 'ทั้งหมด' ตรง ๆ ต้องถูกพากลับมาที่กอง 'ต้องตรวจ' ไม่ใช่เห็นคลังใบทั้งระบบ"""
    html = login("approve").get("/review?filter=all").text
    assert "filter=all" not in html
    assert "ต้องตรวจ" in html


def test_approver_nav_hides_admin_and_search_links(login):
    html = login("approve").get("/").text
    for link in ('href="/search"', 'href="/table"', 'href="/dashboard"', 'href="/staff"'):
        assert link not in html
    assert 'href="/review"' in html


def test_staff_still_sees_search_but_not_admin_pages(login):
    html = login("staff").get("/").text
    assert 'href="/search"' in html
    assert 'href="/table"' not in html
