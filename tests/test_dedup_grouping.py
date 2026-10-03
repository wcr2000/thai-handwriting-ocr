"""Duplicate grouping — no database needed; this tests the union-find logic alone.

Why union-find rather than a group by: three slips can be linked along different edges (A-B
share an image, B-C share a plate). Grouping by one key at a time counts the same group twice,
and the report then overstates how many surplus slips there are.
"""

import datetime as dt

from ocrslip.dedup import group_duplicates, keeper_of

TODAY = dt.date(2026, 9, 28)


SLOT = "อาคาร 1 ชั้น 2 c2"


def slip(sid, *, osha=None, plate=None, tel=None, date=TODAY, status="pending", minute=0,
         location=SLOT, car_status="stored"):
    return {
        "id": sid, "osha": osha, "plate_norm": plate, "tel_digits": tel, "deposit_date": date,
        "review_status": status, "superseded_by": None, "uploaded_by": "ก",
        "name": "ทดสอบ", "plate_raw": plate, "tel": tel, "car_status": car_status,
        "location": location,
        "created_at": dt.datetime(2026, 9, 28, 10, minute),
    }


def ids(groups):
    return sorted(sorted(s["id"] for s in g) for g in groups)


def test_same_original_image_is_one_group():
    rows = [slip("a", osha="H1"), slip("b", osha="H1"), slip("c", osha="H2")]
    assert ids(group_duplicates(rows)) == [["a", "b"]]


def test_same_plate_tel_and_date_is_one_group():
    """One paper slip photographed twice — different hashes, same slip"""
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001"),
            slip("b", osha="H2", plate="1กก1", tel="0810000001")]
    assert ids(group_duplicates(rows)) == [["a", "b"]]


def test_same_paper_slip_photographed_with_sloppy_spacing_still_groups():
    """OCR reads the same parking spot with different spacing; that must not defeat duplicate detection"""
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001", location="อาคาร 1  ชั้น 2 C2"),
            slip("b", osha="H2", plate="1กก1", tel="0810000001", location="อาคาร 1 ชั้น 2 c2")]
    assert ids(group_duplicates(rows)) == [["a", "b"]]


def test_second_round_on_the_same_day_is_not_a_duplicate():
    """Parked in the morning, collected at midday, parked again that evening — plate, phone and date
    all agree, and only the changed parking spot tells them apart.

    This is the case the deposit date cannot catch at all. Flagged as a duplicate, the evening
    slip drops out of the queue, leaving a car genuinely parked with no active slip.
    """
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001", location="อาคาร 1 ชั้น 2 c2"),
            slip("b", osha="H2", plate="1กก1", tel="0810000001", location="อาคาร 3 ชั้น 5 a7")]
    assert group_duplicates(rows) == []


def test_slip_of_a_car_already_returned_never_links_to_a_later_one():
    """A returned slip has closed its own round, so the next one is a new round, even in the same bay"""
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001", car_status="returned"),
            slip("b", osha="H2", plate="1กก1", tel="0810000001")]
    assert group_duplicates(rows) == []


def test_same_car_on_a_different_day_is_not_a_duplicate():
    """The same car parked again on a later day is a fresh deposit and must not be flagged as a duplicate"""
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001", date=TODAY),
            slip("b", osha="H2", plate="1กก1", tel="0810000001",
                 date=TODAY + dt.timedelta(days=30))]
    assert group_duplicates(rows) == []


def test_slips_linked_through_different_keys_land_in_one_group():
    """a-b are linked by image, b-c by plate; all three must land in one group"""
    rows = [slip("a", osha="H1"),
            slip("b", osha="H1", plate="1กก1", tel="0810000001"),
            slip("c", osha="H2", plate="1กก1", tel="0810000001")]
    assert ids(group_duplicates(rows)) == [["a", "b", "c"]]


def test_missing_plate_or_date_never_links_slips():
    """Slips whose plate or date could not be read must not be paired with other equally unreadable slips"""
    rows = [slip("a", osha="H1", plate=None, tel=None, date=None),
            slip("b", osha="H2", plate=None, tel=None, date=None),
            slip("c", osha="H3", plate="1กก1", tel="0810000001", date=None)]
    assert group_duplicates(rows) == []


def test_missing_location_never_links_slips():
    """An unreadable parking spot leaves nothing to separate rounds, so do not infer a duplicate — leave it for a reviewer"""
    rows = [slip("a", osha="H1", plate="1ขข2", tel="0810000002", location=None),
            slip("b", osha="H2", plate="1ขข2", tel="0810000002", location="  ")]
    assert group_duplicates(rows) == []


def test_keeper_is_the_oldest_approved_slip():
    group = [slip("a", minute=0), slip("b", status="approved", minute=5),
             slip("c", status="approved", minute=9)]
    assert keeper_of(group)["id"] == "b"


def test_no_keeper_when_nobody_reviewed_the_group_yet():
    """A group nobody has reviewed has no canonical slip — a reviewer handles one of them normally"""
    assert keeper_of([slip("a"), slip("b", status="rejected")]) is None
