"""The self-service entry form (/in).

This page is open to the public, and its only gate is the passcode a staff member types to close
it out. So the most important test in this file is that the passcode never leaves with the page:
leaked into the HTML sent to a driver's device, anybody could fabricate parking slips from
home.
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip.web import main
from ocrslip.web.main import app

from conftest import TEST_SCHEMA, needs_db

PW = "รหัสทดสอบ-xyz"

GOOD = {
    "name": "สมชาย ใจดี", "tel": "081-234-5678", "date": "2026-09-28",
    "noplate": "1กก 1234", "province": "ปทุมธานี", "brand": "โตโยต้า",
    "typecar": "เก๋ง", "building": "อาคาร 1", "floor": "ชั้น 2",
    "entry_pw": PW,
}


@pytest.fixture
def pw(pgenv, monkeypatch):
    """Set the passcode and point the database at the test database.

    pgenv has to be pulled in as well, ever since /in began reading its building and floor choices
    from the app_settings table. This group of tests previously touched no database and so did not
    need it, but without it now they would open a connection to the DATABASE_URL in .env, which
    points at production.
    """
    monkeypatch.setattr(main, "ENTRY_PASSWORD", PW)


def test_page_is_off_when_no_password_configured(monkeypatch):
    """No passcode set means the page is disabled, which beats accepting data behind no gate at all"""
    monkeypatch.setattr(main, "ENTRY_PASSWORD", "")
    assert TestClient(app).get("/in").status_code == 503
    assert TestClient(app).post("/in", data=GOOD).status_code == 503


def test_open_to_public_without_login(pw):
    """A driver has no account, so the page must open directly rather than bouncing to login"""
    r = TestClient(app).get("/in", follow_redirects=False)
    assert r.status_code == 200


def test_password_never_reaches_the_browser(pw):
    """The passcode must not appear in the HTML, neither on page load nor in the wrong-passcode response.

    This is why the passcode check has to live server-side only. Move it into JavaScript and this
    test fails immediately — which is exactly the point.
    """
    c = TestClient(app)
    assert PW not in c.get("/in").text
    # Wrong passcode: the response must contain neither the real passcode nor the one just typed
    typed = "ที่พิมพ์ผิดไป"  # "the wrong thing that was typed"
    r = c.post("/in", data={**GOOD, "entry_pw": typed})
    assert r.status_code == 200
    assert PW not in r.text
    assert typed not in r.text
    assert "รหัสเจ้าหน้าที่ไม่ถูกต้อง" in r.text


@pytest.mark.parametrize("bad,field,msg", [
    ({"tel": "0812345"}, "tel", "10 หลัก"),
    ({"name": "สมชาย"}, "name", "ชื่อและนามสกุล"),
    ({"noplate": ""}, "noplate", "กรอกทะเบียน"),
    ({"province": "ปทุม"}, "province", "เลือกจังหวัด"),
    ({"building": "ตึกไหนก็ได้"}, "building", "เลือกอาคาร"),
    ({"floor": "ชั้นลอย"}, "floor", "เลือกชั้น"),
])
def test_rejects_bad_input_and_keeps_what_was_typed(pw, bad, field, msg):
    """A rejection must return what was already entered, not clear the form and demand it all be retyped on a phone"""
    r = TestClient(app).post("/in", data={**GOOD, **bad})
    assert r.status_code == 200
    assert msg in r.text
    assert "สมชาย" in r.text or field == "name"


def test_province_and_building_must_come_from_the_list(pw):
    """A value outside the dropdown must be rejected, not trusted because the <select> did not offer it.

    A <select> only constrains people using the page normally; anybody posting directly can send
    whatever they like. Without re-validating server-side, the parking spot becomes arbitrary text
    and cannot be aggregated.
    """
    r = TestClient(app).post("/in", data={**GOOD, "province": "<script>"})
    assert "เลือกจังหวัด" in r.text


# ---------- the part that needs a real Postgres ----------

@pytest.fixture
def clean(pgenv):
    """An empty database, with the building and floor choices back to the code defaults.

    app_settings has to be cleared too, not just slips: GOOD submits "อาคาร 1 / ชั้น 2", which are
    the code defaults, but /in validates against the list read from app_settings. If that table
    still holds test_entry_settings' list (the test database is not recreated every run), /in
    rejects with "choose a parking building" and this whole group fails — and only on the second
    run against the same database, which is the hardest kind of failure to track down.
    """
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.app_settings")
        conn.commit()


@needs_db
def test_accepted_entry_is_searchable_immediately(pw, clean):
    """Typed by the driver and attested by staff = nothing to review, so it must be searchable immediately.

    If these slips still entered the review queue, the labelling bottleneck would not go anywhere,
    and the exit flow would fail to find them, because the search page shows only approved slips.
    """
    r = TestClient(app).post("/in", data=GOOD)
    assert r.status_code == 200
    assert "เจ้าหน้าที่ยืนยันแล้ว" in r.text

    from ocrslip.db import connect
    with connect() as conn:
        row = conn.execute(f"SELECT * FROM {TEST_SCHEMA}.slips").fetchone()
    assert row["review_status"] == "approved"
    assert row["needs_review"] is False
    assert row["entry_source"] == "typed"
    assert row["car_status"] == "stored"
    # The parking spot has to be composed from both dropdowns, not just one of them
    assert row["location"] == "อาคาร 1 ชั้น 2"
    assert row["tel_digits"] == "0812345678"
    # There is no AI cost on this route — a non-zero figure here means somebody reintroduced OCR
    assert row["ocr_cost_usd"] == 0


@needs_db
def test_submitting_twice_does_not_create_a_second_slip(pw, clean):
    """A repeated submit or a page refresh must return the same slip, not give one car two slips.

    A duplicate hurts on the way out: staff see two identical rows and cannot tell which to close.
    Close the wrong one and a slip is stranded that nobody will ever come to collect.
    """
    c = TestClient(app)
    c.post("/in", data=GOOD)
    r = c.post("/in", data={**GOOD, "brand": "ฮอนด้า"})
    assert "ลงทะเบียนไว้แล้ว" in r.text

    from ocrslip.db import connect
    with connect() as conn:
        n = conn.execute(f"SELECT count(*) AS n FROM {TEST_SCHEMA}.slips").fetchone()["n"]
    assert n == 1


@needs_db
def test_same_plate_can_register_again_after_it_left(pw, clean):
    """The same car returning to park after collection must be registrable, not blocked as a duplicate"""
    from ocrslip.db import connect, mark_returned
    c = TestClient(app)
    c.post("/in", data=GOOD)
    with connect() as conn:
        slip_id = conn.execute(f"SELECT id::text AS id FROM {TEST_SCHEMA}.slips").fetchone()["id"]
        assert mark_returned(conn, slip_id, "เจ้าหน้าที่ ก", None) is True
        conn.commit()

    c.post("/in", data=GOOD)
    with connect() as conn:
        n = conn.execute(f"SELECT count(*) AS n FROM {TEST_SCHEMA}.slips").fetchone()["n"]
    assert n == 2


@needs_db
def test_second_release_does_not_overwrite_the_first(pw, clean):
    """Two staff releasing the same car: whoever presses second must not overwrite the first one's record.

    At the checkout point several devices face different ways, and this genuinely happens. Allowed
    to overwrite, the handover name and time become those of whoever pressed last, erasing the
    record of the person who actually released the car.
    """
    from ocrslip.db import connect, mark_returned
    TestClient(app).post("/in", data=GOOD)
    with connect() as conn:
        slip_id = conn.execute(f"SELECT id::text AS id FROM {TEST_SCHEMA}.slips").fetchone()["id"]
        assert mark_returned(conn, slip_id, "คนแรก", None, "ญาติ ก") is True
        assert mark_returned(conn, slip_id, "คนที่สอง", None, "ญาติ ข") is False
        conn.commit()
        row = conn.execute(
            f"SELECT returned_by, released_to FROM {TEST_SCHEMA}.slips WHERE id = %s", (slip_id,)
        ).fetchone()
    assert row["returned_by"] == "คนแรก"
    assert row["released_to"] == "ญาติ ก"


# ---------- mobile ----------

def test_mobile_touch_rule_covers_every_input_type_we_use():
    """Every input type the form actually uses must be covered by the mobile media query's 44px rule.

    That rule once omitted input[type=tel] and input[type=date] — which happen to be exactly the
    phone and date fields on the entry form, leaving those two under 44px tall on a phone. Nobody
    sees a bug like this on a desktop, and the people filling the form in are standing beside a car
    using one thumb.
    """
    import re
    from pathlib import Path

    css = Path("ocrslip/web/static/app.css").read_text(encoding="utf-8")
    forms = " ".join(
        Path(f"ocrslip/web/templates/{n}").read_text(encoding="utf-8")
        for n in ("in.html", "out.html", "out_pick.html", "slip.html", "index.html")
    )
    # radio is deliberately outside this rule: the radios on the exit slip-picker have .pick-row as
    # a whole-row tap target (already taller than 44px), and growing the circle itself to 44px would
    # bloat those short rows past the screen.
    used = set(re.findall(r'<input[^>]*type="(\w+)"', forms)) - {"hidden", "checkbox", "file", "radio"}

    block = css.split("@media (max-width: 820px)", 1)[1].split("}", 1)[0]
    covered = set(re.findall(r"input\[type=(\w+)\]", block))
    assert used <= covered, f"the mobile media query does not yet cover: {sorted(used - covered)}"
