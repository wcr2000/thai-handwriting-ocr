"""The page for comparing already-approved duplicates — one button deletes permanently, so every failure mode needs a test.

What this page must *not* do matters more than what it does:
  * never delete a slip outside the group of the one selected (a form can always be tampered with)
  * never be reachable by anybody but an admin
  * never present a group nobody has reviewed for a decision — the system handles those at approval time

Needs a real Postgres. See tests/conftest.py for how to run it.
"""

import pytest

from conftest import TEST_SCHEMA, needs_db

pytestmark = needs_db

FIELDS = {"tel": "0810000044", "province": "กรุงเทพมหานคร", "brand": "รีโว่",
          "car_type": "เก๋ง", "location": "อาคาร 1 ชั้น 2", "deposit_date": "2026-09-28"}


@pytest.fixture
def approved_twins(pgenv):
    """N approved slips for one paper slip, where the AI read the name differently each time"""
    from ocrslip.db import connect
    from ocrslip.normalize import norm_phone, norm_plate

    def _make(names: tuple[str, ...], plate: str = "กก1234",
              status: str = "approved", **over) -> list[str]:
        with connect() as conn:
            ids = []
            for i, name in enumerate(names):
                ids.append(conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slips
                        (name, tel, tel_digits, plate_raw, plate_norm, province, brand,
                         car_type, location, deposit_date, review_status, needs_review,
                         reviewed_by, reviewed_at, car_status, created_at)
                        VALUES (%(name)s, %(tel)s, %(digits)s, %(plate)s, %(pnorm)s,
                                %(province)s, %(brand)s, %(car_type)s, %(location)s,
                                %(deposit_date)s, %(status)s, false, 'krit', now(),
                                %(car_status)s, now() - (%(age)s || ' min')::interval)
                        RETURNING id::text""",
                    {**FIELDS, "name": name, "plate": plate,
                     "digits": norm_phone(FIELDS["tel"]), "pnorm": norm_plate(plate),
                     "status": status, "car_status": "stored", "age": 100 - i, **over},
                ).fetchone()["id"])
            conn.commit()
        return ids

    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.commit()
    return _make


@pytest.fixture
def link_photo(pgenv):
    """Attach several slips to one shared original image (matching hash).

    Necessary because same_slip()'s text key requires both slips to be un-returned, so a group
    containing an already-collected slip can only be linked by image — which matches reality
    anyway (the same file submitted twice).
    """
    from ocrslip.db import connect

    def _link(ids: list[str], sha: str = "a" * 64) -> None:
        with connect() as conn:
            for slip_id in ids:
                conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slip_images
                        (slip_id, kind, sha256, bytes) VALUES (%s, 'original', %s, %s)""",
                    (slip_id, sha, b"x"),
                )
            conn.commit()

    return _link


def ids_left() -> set[str]:
    from ocrslip.db import connect

    with connect() as conn:
        return {r["id"] for r in
                conn.execute(f"SELECT id::text AS id FROM {TEST_SCHEMA}.slips").fetchall()}


def test_page_shows_the_conflicting_fields(approved_twins, worker):
    a, b = approved_twins(("นายกิตติ อินเพ็ง", "นายกิตติ วันเพ็ง"))
    html = worker("admin").get("/dups").text

    assert "กลุ่มที่ 1 จาก 1" in html
    assert "นายกิตติ อินเพ็ง" in html and "นายกิตติ วันเพ็ง" in html
    # The names differ -> the name row must be highlighted, while the matching phone row must not
    assert 'class="clash"' in html
    assert html.count('class="clash"') == 1, "only genuinely conflicting fields may be highlighted"


def test_keeping_one_slip_deletes_only_its_twins(approved_twins, worker):
    a, b, c = approved_twins(("ก ก", "ข ข", "ค ค"))
    client = worker("admin")

    r = client.post("/dups/resolve", data={"keep": b, "i": 0}, follow_redirects=False)
    assert r.status_code == 303
    assert ids_left() == {b}


def test_a_slip_outside_the_group_is_never_deleted(approved_twins, worker):
    """Another car's slip must survive in every case, even sharing the same database"""
    a, b = approved_twins(("ก ก", "ข ข"))
    outsider = approved_twins(("ค ค",), plate="1กก9999")[0]

    worker("admin").post("/dups/resolve", data={"keep": a, "i": 0}, follow_redirects=False)
    assert outsider in ids_left()
    assert ids_left() == {a, outsider}


def test_forged_form_cannot_delete_across_groups(approved_twins, worker):
    """Whatever ids the form submits, the group is always recomputed server-side"""
    a, b = approved_twins(("ก ก", "ข ข"))
    lonely = approved_twins(("ค ค",), plate="1กก9999")[0]

    # Choosing to keep a slip that is in no duplicate group at all -> there is no group to act on,
    # so nothing may be deleted
    r = worker("admin").post("/dups/resolve", data={"keep": lonely, "i": 0},
                             follow_redirects=False)
    assert r.status_code == 404
    assert ids_left() == {a, b, lonely}


def test_groups_nobody_reviewed_are_not_listed(approved_twins, worker):
    """A group still entirely in the queue is not this page's job — the system clears it at approval time"""
    approved_twins(("ก ก", "ข ข"), status="pending")
    assert "ไม่มีกลุ่มไหนเหลือให้ตัดสินแล้ว" in worker("admin").get("/dups").text


def test_one_approved_plus_one_pending_is_not_listed(approved_twins, worker):
    """One approved slip means nothing conflicts in the archive; the queued one is pulled by mark_superseded"""
    approved_twins(("ก ก",))
    approved_twins(("ข ข",), status="pending")
    assert "ไม่มีกลุ่มไหนเหลือให้ตัดสินแล้ว" in worker("admin").get("/dups").text


def test_page_warns_when_the_group_mixes_stored_and_returned(approved_twins, link_photo, worker):
    a = approved_twins(("ก ก",))[0]
    b = approved_twins(("ข ข",), car_status="returned")[0]
    link_photo([a, b])

    html = worker("admin").get("/dups").text
    assert "ไม่มีใบที่ยัง active เหลือในระบบ" in html


def test_same_photo_group_shows_one_image(approved_twins, link_photo, worker):
    """Matching image hashes across every slip = show one image, not three copies of the same one"""
    ids = approved_twins(("ก ก", "ข ข"))
    link_photo(ids)

    html = worker("admin").get("/dups").text
    assert "ทุกใบในกลุ่มนี้มาจากรูปเดียวกันเป๊ะ" in html
    assert html.count('<img src="/image/') == 1


@pytest.mark.parametrize("who", ["staff", "approve"])
def test_only_admin_can_open_or_resolve(approved_twins, worker, who):
    a, b = approved_twins(("ก ก", "ข ข"))
    client = worker(who)

    assert client.get("/dups").status_code == 403
    assert client.post("/dups/resolve", data={"keep": a, "i": 0},
                       follow_redirects=False).status_code == 403
    assert ids_left() == {a, b}


# ---------- the bulk-resolve button for groups with nothing to decide ----------

def test_bulk_keeps_the_oldest_of_every_identical_group(approved_twins, worker):
    """A group where every field agrees keeps only the oldest — the archive ends up character-for-character the same"""
    a, b, c = approved_twins(("ก ก", "ก ก", "ก ก"))
    x, y = approved_twins(("ข ข", "ข ข"), plate="1กก9999")

    r = worker("admin").post("/dups/resolve-identical", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dups?done=3"
    assert ids_left() == {a, x}


def test_bulk_never_touches_a_group_with_a_conflict(approved_twins, worker):
    """Names read differently means a wrong row in the archive; a person must look at the photo, and bulk-resolve must not touch it"""
    a, b = approved_twins(("นายกิตติ อินเพ็ง", "นายกิตติ วันเพ็ง"))

    r = worker("admin").post("/dups/resolve-identical", follow_redirects=False)
    assert r.headers["location"] == "/dups?done=0"
    assert ids_left() == {a, b}


def test_bulk_skips_groups_that_mix_stored_and_returned(approved_twins, link_photo, worker):
    """Identical data but differing car status — picking the wrong copy changes whether the car is still here, so a person decides"""
    a = approved_twins(("ก ก",))[0]
    b = approved_twins(("ก ก",), car_status="returned")[0]
    link_photo([a, b])

    worker("admin").post("/dups/resolve-identical", follow_redirects=False)
    assert ids_left() == {a, b}


def test_bulk_button_only_shows_when_there_is_something_to_collapse(approved_twins, worker):
    approved_twins(("นายกิตติ อินเพ็ง", "นายกิตติ วันเพ็ง"))
    assert "รวบกลุ่มที่ไม่มีอะไรให้ตัดสิน" not in worker("admin").get("/dups").text

    approved_twins(("ข ข", "ข ข"), plate="1กก9999")
    html = " ".join(worker("admin").get("/dups").text.split())
    assert "รวบกลุ่มที่ไม่มีอะไรให้ตัดสินทีเดียว (1 กลุ่ม / 1 ใบ)" in html


def test_bulk_needs_admin(approved_twins, worker):
    a, b = approved_twins(("ก ก", "ก ก"))
    assert worker("staff").post("/dups/resolve-identical",
                                follow_redirects=False).status_code == 403
    assert ids_left() == {a, b}


def test_whitespace_only_difference_is_not_a_conflict(approved_twins, worker):
    """'1ขก1111' and '1ขก 1111' are the same plate and must never be put to a person.

    Observed on the live page: two slips from the same photo (matching hash), differing only in
    where the AI put spaces in the plate and the phone number. This page used to highlight that as
    a conflict even though plate_norm and tel_digits in the archive were byte-identical, leaving
    somebody clicking through groups with nothing in them to decide.
    """
    a, b = approved_twins(("นภา ทดสอบ", "นภา ทดสอบ"))
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET tel = '0890000127', "
                     f"plate_raw = '1ขก1111' WHERE id = %s", (a,))
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET tel = '089 000 0127', "
                     f"plate_raw = '1ขก 1111' WHERE id = %s", (b,))
        conn.commit()

    html = worker("admin").get("/dups").text
    assert 'class="clash"' not in html, "differing whitespace is not a conflict"
    assert "รวบกลุ่มที่ไม่มีอะไรให้ตัดสินทีเดียว" in html


def test_bulk_collapses_a_whitespace_only_group(approved_twins, worker):
    """And such a group must be clearable by the bulk-resolve button, not left for somebody to click"""
    a, b = approved_twins(("นภา ทดสอบ", "นภา ทดสอบ"))
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET plate_raw = '1ขก1111' WHERE id = %s", (a,))
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET plate_raw = '1ขก 1111' WHERE id = %s", (b,))
        conn.commit()

    worker("admin").post("/dups/resolve-identical", follow_redirects=False)
    assert ids_left() == {a}, "only the oldest slip may remain"


def test_a_real_difference_is_still_a_conflict(approved_twins, worker):
    """Guard against overcorrecting: plates differing by an actual character must still count as a conflict"""
    a, b = approved_twins(("นภา ทดสอบ", "นภา ทดสอบ"))
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET plate_raw = '1ขก1111' WHERE id = %s", (a,))
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET plate_raw = '1ขข 1111' WHERE id = %s", (b,))
        conn.commit()

    html = worker("admin").get("/dups").text
    assert html.count('class="clash"') == 1
