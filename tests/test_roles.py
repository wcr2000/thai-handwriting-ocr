"""Per-role permissions — break these and outsiders see every car owner's personal data.

The role needing the tightest guard is "approver" (the volunteers who came to help label):
they must be able to do exactly two things, upload and approve slips in the queue.
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
        assert r.status_code == 303, f"login as {username} failed"
        return c

    return _login


def test_approve_account_gets_approver_role(login):
    user, err = auth.authenticate("approve", TEST_PW, "10.0.0.9")
    assert err == ""
    assert user.is_approver and not user.is_admin


def test_unknown_role_in_token_is_not_treated_as_staff():
    """A token carrying no role (an old one) must fall back to the least privilege, not to staff"""
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


def test_approver_can_reject_but_not_return(login):
    """Rejecting is part of reviewing (the reviewer sees the blurred photo themselves), but releasing a car stays closed.

    Staff can now release cars (the person at checkout is staff, not an admin), but an approver
    still must not — they never see the car and never meet the owner.
    What enforces this is the approver allowlist, not ADMIN_ONLY_SUFFIX, which is now empty.
    """
    c = login("approve")
    slip_id = "00000000-0000-0000-0000-000000000000"
    assert c.post(f"/review/{slip_id}/reject", data={"reason": "x"}).status_code != 403
    assert c.post(f"/slips/{slip_id}/return", data={}).status_code == 403


def test_approver_queue_falls_back_to_pending_pile(login):
    """Requesting the "all" pile directly must redirect back to "needs review", not expose the whole archive"""
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


def test_approve_without_reviewer_name_is_blocked(login):
    """The account is shared by several people; without requiring a real name, there is no way to know who approved what.

    This test confirms the request is *blocked*, so nothing is written to the DB — the slip stays
    in the queue as it was.
    """
    from ocrslip.db import connect, get_slip, list_slips

    with connect() as conn:
        pending = list_slips(conn, review_status="pending", limit=1)
    if not pending:
        pytest.skip("no pending slips in the DB yet")
    slip_id = str(pending[0]["id"])

    r = login("approve").post(
        f"/review/{slip_id}/approve",
        data={"name": "ทดสอบ ระบบ", "tel": "0812345678", "noplate": "กก1234", "reviewed_by": ""},
        follow_redirects=False,
    )
    assert r.status_code == 200, "must re-render the page with an error, not redirect as though approved"
    assert "ต้องระบุชื่อผู้ตรวจ" in r.text
    with connect() as conn:
        assert get_slip(conn, slip_id)["review_status"] == "pending"


def test_reviewer_sentinel_never_becomes_a_person_name(login):
    """Choosing "+ new name" and typing nothing must not be recorded as a person called __new__"""
    from ocrslip.db import connect, get_slip, list_slips

    with connect() as conn:
        pending = list_slips(conn, review_status="pending", limit=1)
    if not pending:
        pytest.skip("no pending slips in the DB yet")
    slip_id = str(pending[0]["id"])

    r = login("approve").post(
        f"/review/{slip_id}/approve",
        data={"name": "ทดสอบ ระบบ", "tel": "0812345678", "noplate": "กก1234",
              "reviewed_by": "__new__", "reviewed_by_new": "   "},
        follow_redirects=False,
    )
    assert r.status_code == 200 and "ต้องระบุชื่อผู้ตรวจ" in r.text
    with connect() as conn:
        assert get_slip(conn, slip_id)["review_status"] == "pending"
