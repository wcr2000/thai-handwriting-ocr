"""The route into "edit an already-approved slip" has to be reachable from the pages staff actually use.

/review/{id}?edit=1 already worked in full, but nothing in the system linked to it. Slips from
the entry form (/in) enter the approved state immediately, so a "review" link never appeared for
them anywhere, leaving people on the ground to type ?edit=1 after a UUID themselves — which
amounts to the feature not existing.

So this suite treats "there is a link to click" as the thing that must not regress, rather than
merely that the route answers 200.
"""

import datetime

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
def typed_slip(pgenv):
    """A self-service slip — approved from the outset, with no photo and no AI confidence scores.

    These are the slips most often returned to for an edit (people mistype their own phone number).
    """
    from ocrslip.db import connect

    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        sid = conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slips
                (name, name_norm, tel, tel_digits, plate_raw, plate_norm, province,
                 brand, car_type, location, deposit_date,
                 review_status, needs_review, entry_source)
                VALUES ('สมชาย ใจดี', 'สมชายใจดี', '0875000999', '0875000999',
                        '5 ขก 1234', '5ขก1234', 'กรุงเทพ', 'Honda', 'suv',
                        'อาคาร 3 ชั้น 2', current_date, 'approved', false, 'typed')
                RETURNING id::text""").fetchone()["id"]
        conn.commit()
    return sid


def test_slip_page_links_to_the_edit_form(typed_slip):
    """The slip page needs an edit button — it is the only page staff reach by searching"""
    html = _login("staff").get(f"/slips/{typed_slip}").text
    assert f"/review/{typed_slip}?edit=1" in html


def test_search_result_links_to_the_edit_form(typed_slip):
    """A wrong phone number usually surfaces on the way out (the call does not connect), with the user on the search page, not the review queue"""
    html = _login("staff").get("/api/search?q=5ขก1234").text
    assert f"/review/{typed_slip}?edit=1" in html


def test_table_row_links_to_the_edit_form(typed_slip):
    """Table rows used to link only for pending slips, leaving approved ones a dead end"""
    html = _login("admin").get("/table").text
    assert f"/review/{typed_slip}?edit=1" in html


def test_edit_form_of_approved_slip_is_editable_not_readonly(typed_slip):
    """Arriving via ?edit=1 must give the save button bar, not a read-only "already reviewed" page"""
    html = _login("staff").get(f"/review/{typed_slip}?edit=1").text
    assert 'class="actbar" hidden' not in html and "<div class=\"actbar\" hidden>" not in html
    # force skips the optimistic lock, because this is a deliberate return to edit a reviewed slip
    assert 'name="force" value="1"' in html


def test_edit_form_of_typed_slip_has_no_broken_image(typed_slip):
    """A self-service slip has no evidence image, so there must be no <img> pointing at /image to give a half-screen broken frame"""
    html = _login("staff").get(f"/review/{typed_slip}?edit=1").text
    assert f"/image/{typed_slip}" not in html
    assert "ความมั่นใจของ AI" not in html
    assert "AI มั่นใจทุกช่อง" not in html


def test_edit_form_keeps_a_car_type_that_is_not_in_the_dropdown(typed_slip):
    """The entry form lets people type their own vehicle type (suv), so the edit page's list must include that value.

    With no option to mark selected, the <select> posts back an empty value — so opening the slip
    to fix a phone number silently erases the vehicle type at the same time.
    """
    html = _login("staff").get(f"/review/{typed_slip}?edit=1").text
    assert '<option value="suv" selected>suv</option>' in html.replace(" >", ">")


def test_edit_form_of_approved_slip_has_no_reject_button(typed_slip):
    """Somebody who came to fix a phone number should not find a reject button waiting below the form.

    Rejecting a slip whose car is genuinely parked drops it out of the search results, and it
    cannot be found when the owner arrives to collect.
    """
    html = _login("staff").get(f"/review/{typed_slip}?edit=1").text
    assert f"/review/{typed_slip}/reject" not in html


def test_saving_fixes_the_field_and_keeps_the_slip_approved(typed_slip):
    """What must actually happen: the number changes, the status holds, and we know who edited it"""
    from ocrslip.db import connect

    c = _login("staff")
    r = c.post(f"/review/{typed_slip}/approve", data={
        # The fixture seeds deposit_date = current_date, so post today's date back
        # unchanged. Hard-coding a date here would log a spurious deposit_date edit
        # on every day but the one this test was written on.
        "name": "สมชาย ใจดี", "tel": "0875000996",
        "date": datetime.date.today().strftime("%d/%m/%Y"),
        "noplate": "5 ขก 1234", "province": "กรุงเทพ", "brand": "Honda",
        "typecar": "suv", "location": "อาคาร 3 ชั้น 2",
        "reviewed_by": "__new__", "reviewed_by_new": "เจ้าหน้าที่ ทดสอบ",
        "force": "1",
    }, follow_redirects=False)
    assert r.status_code == 303

    with connect() as conn:
        row = conn.execute(
            f"SELECT tel, tel_digits, review_status FROM {TEST_SCHEMA}.slips WHERE id = %s",
            (typed_slip,)).fetchone()
        edits = conn.execute(
            f"""SELECT field, old_value, new_value, edited_by FROM {TEST_SCHEMA}.slip_edits
                WHERE slip_id = %s""", (typed_slip,)).fetchall()

    assert row["tel"] == "0875000996"
    # If tel_digits does not follow, searching by the new number finds nothing — which was the
    # entire reason for the edit
    assert row["tel_digits"] == "0875000996"
    assert row["review_status"] == "approved"
    assert [(e["field"], e["old_value"], e["new_value"]) for e in edits] == [
        ("tel", "0875000999", "0875000996")]
    assert edits[0]["edited_by"] == "เจ้าหน้าที่ ทดสอบ"


def test_approver_account_still_cannot_reach_the_slip_page(typed_slip):
    """The new link must not open a door for the approver role, limited to upload and the review queue"""
    assert _login("approve").get(f"/slips/{typed_slip}").status_code == 403
