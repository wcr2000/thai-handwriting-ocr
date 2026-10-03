"""Stop pages being cached into users seeing an old version of a form after a deploy.

This happened for real: the old form Safari had cached lacked a field the new server required,
so submitting it produced an error the user could not possibly fix, because the field was not
there to fill in.
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip.web.main import app

client = TestClient(app)

# A test-only password. The tests set the account env vars themselves every time, so no real
# password lives in git and the tests do not break every time the production password changes.
TEST_PW = "pw-for-test"


@pytest.fixture
def accounts(monkeypatch):
    """Load a test account for every role into the environment and return a login helper"""
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
        assert r.status_code == 303, f"login as {username} failed"
        return c

    return login


def test_html_is_never_cached():
    r = client.get("/login")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")


def test_redirect_to_login_is_not_cached():
    """The 303 to the login page must not be cached either, or an already-logged-in user keeps getting bounced out.

    Never write this as `assert A or r.headers.get("location")`: location always has a value, so
    the test would stay green while checking nothing at all.
    """
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
    assert "no-store" in r.headers.get("cache-control", "").lower()


def test_forbidden_page_is_not_cached(accounts):
    """The 403 page auth_gate returns itself needs the header too, or it sits cached even after permissions change.

    A separate client is used, because TestClient keeps cookies across tests. Left logged in, the
    other test asserting "an unauthenticated request gets bounced" would fail as a result.
    """
    staff_client = accounts("staff")
    r = staff_client.get("/table", follow_redirects=False)
    assert r.status_code == 403
    assert "no-store" in r.headers.get("cache-control", "").lower()


def test_static_must_revalidate():
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
    assert r.headers.get("etag"), "an ETag is required, or the file is re-downloaded every time"


def test_static_still_answers_304_with_etag():
    """no-cache means "revalidate before use", not "do not store" — a 304 must still be possible, to spare mobile data"""
    etag = client.get("/static/app.css").headers["etag"]
    r = client.get("/static/app.css", headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert not r.content


def test_image_keeps_its_own_cache_policy(accounts):
    """Evidence images never change, so they may be cached for a long time — the middleware must not override that"""
    from ocrslip.config import DB_SCHEMA
    from ocrslip.db import connect

    # This has to pick a slip that *has* an image, not merely the most recent one: self-service
    # slips (entry_source typed) carry no evidence image, and if the latest slip is one of those,
    # /image answers 404 and the test fails for the wrong reason.
    with connect() as conn:
        row = conn.execute(
            f"""SELECT s.id::text AS id FROM {DB_SCHEMA}.slips s
                 WHERE EXISTS (SELECT 1 FROM {DB_SCHEMA}.slip_images i WHERE i.slip_id = s.id)
                 ORDER BY s.created_at DESC LIMIT 1"""
        ).fetchone()
    if not row:
        pytest.skip("no slip with an evidence image in the DB yet")
    r = accounts("admin").get(f"/image/{row['id']}")
    assert "max-age" in r.headers.get("cache-control", "")
    assert "no-store" not in r.headers.get("cache-control", "")


@pytest.mark.parametrize("path", ["/", "/review", "/search", "/table", "/dashboard", "/staff"])
def test_pages_require_login(path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")
