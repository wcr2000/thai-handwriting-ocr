"""Permanent slip deletion — the only irreversible button in the system.

It exists for test slips and nonsense entries, which only skew the summary figures if kept.
What has to be locked down tightly is *who* can delete, because a mistake cannot be recovered.
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
    """One slip with an evidence image and an edit history, to confirm the dependants really go with it"""
    from ocrslip.db import connect

    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        sid = conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slips (name, tel, plate_raw, plate_norm)
                VALUES ('ทดสอบ ลบ', '0800000000', '9กก 9999', '9กก9999')  -- "delete test"
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
    """Staff can release a car but cannot delete a slip: releasing the wrong car is recoverable, deleting is not"""
    assert _login(username).post(f"/slips/{slip}/delete").status_code == 403
    assert _counts(slip)[0] == 1, "the slip must still exist"


def test_admin_delete_removes_the_slip_and_everything_attached(slip):
    """Deleting a slip must take its evidence images and edit history with it, not leave orphaned remains"""
    assert _counts(slip) == (1, 1, 1)
    r = _login("admin").post(f"/slips/{slip}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/search?deleted=")
    assert _counts(slip) == (0, 0, 0)


def test_deleting_the_same_slip_twice_is_not_a_crash(slip):
    """Pressing back and submitting again must give a comprehensible 404, not a 500"""
    c = _login("admin")
    c.post(f"/slips/{slip}/delete", follow_redirects=False)
    r = c.post(f"/slips/{slip}/delete", follow_redirects=False)
    assert r.status_code == 404


def test_delete_box_is_hidden_from_non_admin(slip):
    """Staff must not even see the delete box: a visible button they cannot press is an invitation to try"""
    assert "ลบใบนี้ถาวร" not in _login("staff").get(f"/slips/{slip}").text
    assert "ลบใบนี้ถาวร" in _login("admin").get(f"/slips/{slip}").text
