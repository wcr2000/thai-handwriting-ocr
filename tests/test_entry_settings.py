"""Configuring the entry form's building and floor choices from the web UI.

The reason this moved onto the web UI: each event opens a different set of buildings, and the
people who know which ones are open today are the people on the ground, not whoever holds the
Render dashboard. So these tests focus on two things: an edit takes effect immediately with no
restart, and only an admin may make one.
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
    """Clear any existing settings, then return a client logged in as admin"""
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
    """This page changes what the public sees on a public form, so it must be admin-only"""
    c = _login(username)
    assert c.get("/settings", follow_redirects=False).status_code == 403
    assert c.post("/settings", data={"buildings": "x", "floors": "y"}).status_code == 403


def test_saved_buildings_show_up_in_the_public_form_right_away(admin):
    """An edit must take effect immediately — needing a restart to see it would be no better than setting it in env"""
    admin.post("/settings", data={"buildings": "อาคารบุญ\nอาคารธรรม", "floors": "ชั้น 1\nชั้น 2"})

    html = TestClient(app).get("/in").text
    assert "อาคารบุญ" in html and "อาคารธรรม" in html
    # The env defaults must disappear rather than being appended, leaving both sets present
    assert "ลานจอดรอบนอก" not in html


def test_clearing_the_boxes_falls_back_to_env_defaults(admin):
    """Clearing the field falls back to .env, rather than setting the list *to* empty over the default.

    Storing an empty string instead of deleting the row would leave the entry form with a dropdown
    holding no options at all, and nobody could register for the whole event.
    """
    admin.post("/settings", data={"buildings": "อาคารเดียว", "floors": "ชั้นเดียว"})
    admin.post("/settings", data={"buildings": "   \n  ", "floors": ""})

    html = TestClient(app).get("/in").text
    assert "อาคารเดียว" not in html
    assert main.ENTRY_BUILDINGS[0] in html


def test_public_form_rejects_a_building_that_is_no_longer_offered(admin):
    """Server-side validation always uses the *current* list, not the list as of when the page rendered.

    Somebody leaves the page open from the morning, and an admin closes that building in the
    afternoon. A value submitted afterwards must be rejected, or we get a slip naming a parking
    spot that was not in use that day.
    """
    admin.post("/settings", data={"buildings": "อาคารบุญ", "floors": "ชั้น 1"})

    c = TestClient(app)
    r = c.post("/in", data={**GOOD, "building": "อาคาร 1", "floor": "ชั้น 1"})
    assert "เลือกอาคาร" in r.text

    r = c.post("/in", data={**GOOD, "building": "อาคารบุญ", "floor": "ชั้น 1"})
    assert "เจ้าหน้าที่ยืนยันแล้ว" in r.text


def test_settings_page_never_offers_to_edit_the_entry_password(admin):
    """The staff passcode belongs in .env alone. Being editable from the web UI would mean storing it
    readably in the database, and the secret would then travel with every backup.
    """
    html = admin.get("/settings").text
    assert ENTRY_PW not in html
    assert 'name="entry_pw"' not in html
    assert 'name="entry_password"' not in html


def test_saved_quotes_show_up_on_the_slip_right_away(admin):
    """A quote the team sets must appear immediately on the slip the driver screenshots.

    28/09/2026 is a Monday. A single quote line is used so there is no doubt which line comes out,
    with no need to work out where that date lands in the rotation.
    """
    admin.post("/settings", data={"buildings": "อาคารบุญ", "floors": "ชั้น 1",
                                  "quotes": "คำคมของทีมเราเอง"})

    r = TestClient(app).post("/in", data={**GOOD, "building": "อาคารบุญ", "floor": "ชั้น 1"})
    assert "คำคมของทีมเราเอง" in r.text
    # The band no longer prints the weekday as text, leaving the colour to convey the day — so
    # assert on the colour instead. #a67c00 is Monday's colour in DAY_COLORS; if the band ever
    # fell back to the colour of the day the page was opened, this test would catch it.
    assert "#a67c00" in r.text


def test_clearing_the_quotes_falls_back_to_the_built_in_set(admin):
    """Clearing the quotes entirely must fall back to the set shipped with the system, not give an empty band or a broken page.

    This is the final page of registration: if it breaks, the car is already parked but there is
    no slip to screenshot.
    """
    admin.post("/settings", data={"buildings": "อาคารบุญ", "floors": "ชั้น 1",
                                  "quotes": "คำคมของทีมเราเอง"})
    admin.post("/settings", data={"buildings": "อาคารบุญ", "floors": "ชั้น 1", "quotes": "  \n "})

    r = TestClient(app).post("/in", data={**GOOD, "building": "อาคารบุญ", "floor": "ชั้น 1"})
    assert "คำคมของทีมเราเอง" not in r.text
    assert day_stamp(dt.date(2026, 9, 28))["quote"] in r.text
