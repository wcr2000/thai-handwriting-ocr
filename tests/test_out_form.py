"""The self-service exit form (/out).

Like /in, this page is open to the public, and it genuinely closes a slip (releases a car). The
two most important tests in this file:
  * the staff passcode must never leave with the page — leaked, anybody could release somebody
    else's car from home
  * a wrong passcode must reveal nothing about any slip — otherwise /out becomes a tool for
    probing which plates are parked here without knowing the passcode at all
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip.web import main
from ocrslip.web.main import app

from conftest import TEST_SCHEMA, needs_db

PW = "รหัสทดสอบ-xyz"

# The entry slip every test in this file starts from, created by posting to /in for real rather
# than inserting a row directly — because what is under test is whether the exit flow finds the
# slip the entry flow created. Inserting the row by hand would hide any bug where the two sides
# normalize differently.
IN = {
    "name": "สมชาย ใจดี", "tel": "081-234-5678", "date": "2026-09-28",
    "noplate": "1กก 1234", "province": "ปทุมธานี", "brand": "โตโยต้า",
    "typecar": "เก๋ง", "building": "อาคาร 1", "floor": "ชั้น 2",
    "entry_pw": PW,
}
OUT = {"tel": "0812345678", "noplate": "1กก1234", "entry_pw": PW}


@pytest.fixture
def pw(pgenv, monkeypatch):
    monkeypatch.setattr(main, "ENTRY_PASSWORD", PW)


@pytest.fixture
def clean(pgenv):
    """An empty database, with the building and floor choices back to the code defaults.

    app_settings has to be cleared too, not just slips: this file creates its starting slip by
    posting to /in for real, which validates the submitted building and floor against the list read
    from app_settings. If another test file (test_entry_settings) left its list in that table, /in
    rejects with "choose a parking building" and every test here fails for a reason that has
    nothing to do with the exit flow — and only when the whole suite runs, which is the hardest
    kind of failure to track down.
    """
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.app_settings")
        conn.commit()


def _row():
    from ocrslip.db import connect
    with connect() as conn:
        return conn.execute(f"SELECT * FROM {TEST_SCHEMA}.slips").fetchone()


# ---------- this page's gate ----------

def test_page_is_off_when_no_password_configured(monkeypatch):
    """No passcode set means the page is disabled, which beats allowing cars to be released behind no gate at all"""
    monkeypatch.setattr(main, "ENTRY_PASSWORD", "")
    assert TestClient(app).get("/out").status_code == 503
    assert TestClient(app).post("/out", data=OUT).status_code == 503


def test_open_to_public_without_login(pw):
    """A driver has no account, so the page must open directly rather than bouncing to login"""
    assert TestClient(app).get("/out", follow_redirects=False).status_code == 200


def test_password_never_reaches_the_browser(pw):
    c = TestClient(app)
    assert PW not in c.get("/out").text
    typed = "ที่พิมพ์ผิดไป"  # "the wrong thing that was typed"
    r = c.post("/out", data={**OUT, "entry_pw": typed})
    assert r.status_code == 200
    assert PW not in r.text and typed not in r.text
    assert "รหัสเจ้าหน้าที่ไม่ถูกต้อง" in r.text


@needs_db
def test_wrong_password_says_nothing_about_the_slip(pw, clean):
    """A wrong passcode must not reveal whether a slip exists for this plate, and must close nothing.

    Answering "no slip found for this plate" to somebody who got the passcode wrong already gives
    away half of which plates are parked here — information that can be used to track a person. So
    the passcode is always checked before any lookup.
    """
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/out", data={**OUT, "entry_pw": "มั่ว"})
    assert "สมชาย" not in r.text
    assert "ไม่พบใบ" not in r.text
    assert _row()["car_status"] == "stored"


# ---------- the normal path ----------

@needs_db
def test_closes_the_slip_and_shows_a_receipt(pw, clean):
    """A matching phone and plate plus staff attestation closes the slip and yields a summary page to screenshot"""
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/out", data=OUT)
    assert r.status_code == 200
    assert "นำรถออกแล้ว" in r.text
    assert "แคปหน้าจอ" in r.text

    row = _row()
    assert row["car_status"] == "returned"
    assert row["returned_at"] is not None
    assert row["returned_by"] == main.RETURNED_BY_OUT
    # released_to must stay empty: that field means "collected by somebody other than the owner",
    # and filling in the name from the slip would amount to recording that we checked somebody's
    # ID, which this route does not do.
    assert row["released_to"] is None


@needs_db
@pytest.mark.parametrize("plate", ["1กก1234", "1กก 1234", " 1กก-1234 ", "1กก.1234",
                                  "1กก1234 ปทุมธานี"])
def test_plate_matches_regardless_of_spacing_and_province(pw, clean, plate):
    """Spaces, dashes, dots and a trailing province must not stop the lookup finding the slip.

    The exit flow compares against plate_norm, normalized at registration time, not the raw text.
    Compared raw, somebody who spaced their plate differently from the way in would be blocked
    despite genuinely owning the car.
    """
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/out", data={**OUT, "noplate": plate})
    assert "นำรถออกแล้ว" in r.text, plate


@needs_db
@pytest.mark.parametrize("tel", ["0812345678", "081-234-5678", "081 234 5678",
                                 "+66812345678"])
def test_phone_matches_regardless_of_formatting(pw, clean, tel):
    c = TestClient(app)
    c.post("/in", data=IN)
    assert "นำรถออกแล้ว" in c.post("/out", data={**OUT, "tel": tel}).text


@needs_db
def test_both_fields_must_match_not_just_one(pw, clean):
    """A correct plate with the wrong phone fails and must close nothing (the conditions really are ANDed)"""
    c = TestClient(app)
    c.post("/in", data=IN)
    r = c.post("/out", data={**OUT, "tel": "0899999999"})
    assert "เบอร์โทรไม่ตรง" in r.text
    assert _row()["car_status"] == "stored"


@needs_db
def test_unknown_plate_and_already_returned_say_different_things(pw, clean):
    """"No slip found" and "already collected" have to be different messages.

    The remedies are different: the first means check what you typed, the second means the car has
    genuinely gone. Given the same message, somebody whose car has already left stands there
    re-entering their details with no idea what happened.
    """
    c = TestClient(app)
    c.post("/in", data=IN)

    r = c.post("/out", data={**OUT, "noplate": "9ขข9999"})
    assert "ไม่พบใบจอดของทะเบียนนี้" in r.text

    assert "นำรถออกแล้ว" in c.post("/out", data=OUT).text
    r = c.post("/out", data=OUT)
    assert "รับรถกลับไปแล้วเมื่อ" in r.text
    assert "นำรถออกแล้ว" not in r.text


@needs_db
def test_slip_without_a_phone_on_file_passes_on_plate_alone(pw, clean):
    """A slip whose phone number OCR could not read must still be releasable on plate plus staff passcode.

    Requiring a phone match on every slip would block the genuine owner over data we ourselves
    could not read — and staff would then release the car recording nothing at all, which is
    worse.
    """
    from ocrslip.db import connect
    c = TestClient(app)
    c.post("/in", data=IN)
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET tel = NULL, tel_digits = NULL")
        conn.commit()
    assert "นำรถออกแล้ว" in c.post("/out", data=OUT).text


@needs_db
def test_rejected_and_superseded_slips_are_not_pickable(pw, clean):
    """A rejected or duplicate-flagged slip is not a real deposit and must not count as awaiting collection"""
    from ocrslip.db import connect
    c = TestClient(app)
    c.post("/in", data=IN)
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET review_status = 'rejected'")
        conn.commit()
    assert "ไม่พบใบจอดของทะเบียนนี้" in c.post("/out", data=OUT).text


# ---------- one plate with several still-parked slips ----------

@needs_db
def test_two_open_slips_ask_which_one_instead_of_guessing(pw, clean):
    """Two still-parked slips means offering a choice, not guessing which one to close.

    Close the wrong one and a slip is stranded that nobody will ever come to collect — silently,
    and undiscoverably.
    """
    from ocrslip.db import connect
    c = TestClient(app)
    c.post("/in", data=IN)
    with connect() as conn:
        # A second slip for the same plate (a different deposit date), imitating a duplicate that
        # genuinely escaped the duplicate check
        conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slips
                (name, tel, tel_digits, plate_raw, plate_norm, deposit_date, location,
                 review_status, needs_review, car_status)
                VALUES ('สมชาย ใจดี', '0812345678', '0812345678', '1กก 1234', '1กก1234',
                        '2026-09-20', 'อาคาร 2 ชั้น 1', 'approved', false, 'stored')""")
        conn.commit()

    r = c.post("/out", data=OUT)
    assert "มีใบที่ยังจอดอยู่ 2 ใบ" in r.text
    assert PW not in r.text, "the slip-picker page must not carry the staff passcode"
    # Nothing may be preselected: this page appears precisely because the system cannot guess.
    # Preselect one and somebody in a hurry confirms without reading, which is exactly the
    # wrong-slip closure this page exists to prevent.
    assert "checked" not in r.text
    # The latest round (deposited 28/09) must sit above the older one (deposited 20/09) on the page
    assert r.text.index("28/09/2026") < r.text.index("20/09/2026")
    with connect() as conn:
        n = conn.execute(
            f"SELECT count(*) AS n FROM {TEST_SCHEMA}.slips WHERE car_status = 'stored'"
        ).fetchone()["n"]
    assert n == 2, "while asking which slip, none may be closed yet"


@needs_db
def test_picking_a_slip_id_from_another_car_is_rejected(pw, clean):
    """Posting another car's slip_id alongside one's own plate must not close that slip.

    slip_id comes from the picker page and is therefore client-supplied, so it must never be
    trusted directly: it has to appear in the results for the plate and phone submitted in that
    same request.
    """
    from ocrslip.db import connect
    c = TestClient(app)
    c.post("/in", data=IN)
    victim = _row()["id"]
    other = {**IN, "tel": "0899999999", "noplate": "9ขข 9999", "name": "สมหญิง ใจงาม"}
    c.post("/in", data=other)

    r = c.post("/out", data={"tel": "0899999999", "noplate": "9ขข9999",
                             "slip_id": str(victim), "entry_pw": PW})
    # Their own single slip is closed as normal, but the slip whose id was injected must be untouched
    assert "นำรถออกแล้ว" in r.text
    with connect() as conn:
        row = conn.execute(
            f"SELECT car_status FROM {TEST_SCHEMA}.slips WHERE id = %s", (victim,)
        ).fetchone()
    assert row["car_status"] == "stored"


@needs_db
def test_second_press_does_not_overwrite_the_first_release(pw, clean):
    """A repeated confirm, or staff having closed it from the slip page first, must not overwrite the first record"""
    from ocrslip.db import connect, mark_returned
    c = TestClient(app)
    c.post("/in", data=IN)
    slip_id = str(_row()["id"])
    with connect() as conn:
        assert mark_returned(conn, slip_id, "เจ้าหน้าที่ ก", None) is True
        conn.commit()

    r = c.post("/out", data=OUT)
    assert "นำรถออกแล้ว" not in r.text
    assert _row()["returned_by"] == "เจ้าหน้าที่ ก"
