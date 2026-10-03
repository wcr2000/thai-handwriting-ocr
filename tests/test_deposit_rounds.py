"""For a car parked over several rounds, it must be obvious which row or slip is which round.

Origin: people parked, collected, and parked again — two or three rounds per car has been
observed. The data was already correct (one slip = one round), but the search page returned
near-identical rows ordered by score, leaving exit staff to read the dates themselves to work out
which slip is the current round. Close the wrong one and a slip is stranded that nobody will ever
come to collect.

Needs a real Postgres: what is under test is a window function and the row order SQL returns.
See tests/conftest.py for how to run it.
"""

import pytest

from conftest import TEST_SCHEMA, needs_db

pytestmark = needs_db

PLATE, TEL = "กก1234", "0810000044"


@pytest.fixture
def rounds(pgenv):
    """Create N rounds for one plate (one per day, oldest first), returning ids in round order"""
    from ocrslip.db import connect
    from ocrslip.normalize import norm_phone, norm_plate

    def _make(n: int = 3, *, plate: str = PLATE, returned_except_last: bool = True):
        with connect() as conn:
            ids = []
            for i in range(n):
                last = i == n - 1
                ids.append(conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slips
                        (name, tel, tel_digits, plate_raw, plate_norm, deposit_date, location,
                         review_status, needs_review, car_status, returned_at, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'approved', false, %s, %s,
                                now() - (%s || ' day')::interval)
                        RETURNING id::text""",
                    ("สมหมาย ทดสอบ", TEL, norm_phone(TEL), plate, norm_plate(plate),
                     f"2026-09-{10 + i:02d}", f"อาคาร {i + 1} ชั้น 2",
                     "stored" if (last or not returned_except_last) else "returned",
                     None if (last or not returned_except_last) else "2026-09-20",
                     n - i),
                ).fetchone()["id"])
            conn.commit()
        return ids

    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.commit()
    return _make


@pytest.fixture
def dup_day(rounds):
    """N slips for one plate on the *same day* = one paper slip uploaded repeatedly.

    The parking spot and car status are deliberately made to differ — that is precisely how these
    escaped the duplicate check at approval time in production (db.same_slip() requires a matching
    parking spot and both slips un-returned).
    """
    from ocrslip.db import connect
    from ocrslip.normalize import norm_phone, norm_plate

    assert rounds  # lean on the existing fixture to clear the table, rather than two places knowing how

    def _make(n: int = 3, *, day: str | None = "2026-09-26"):
        with connect() as conn:
            ids = [conn.execute(
                f"""INSERT INTO {TEST_SCHEMA}.slips
                    (name, tel, tel_digits, plate_raw, plate_norm, deposit_date, location,
                     review_status, needs_review, car_status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'approved', false, %s,
                            now() + (%s || ' second')::interval)
                    RETURNING id::text""",
                ("สมศรี ทดสอบ", TEL, norm_phone(TEL), PLATE, norm_plate(PLATE), day,
                 ["", "ดำ", " ดำ "][i % 3], "returned" if i == 2 else "stored", i),
            ).fetchone()["id"] for i in range(n)]
            conn.commit()
        return ids

    return _make


def test_a_car_deposited_once_gets_no_round_label(rounds):
    """A lone slip needs no "round 1 of 1" badge — it is pure clutter"""
    from ocrslip.db import connect, deposit_rounds

    only = rounds(1)
    with connect() as conn:
        assert deposit_rounds(conn, [_plate_norm()]) == {}
        assert only  # the slip does exist; it simply carries no badge


def test_rounds_are_numbered_by_deposit_date_oldest_first(rounds):
    """Round 1 must be the first deposit, not whichever slip was recorded in the system first"""
    from ocrslip.db import connect, deposit_rounds

    first, second, third = rounds(3)
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm()])

    assert [got[i]["round"] for i in (first, second, third)] == [1, 2, 3]
    assert {got[i]["total"] for i in (first, second, third)} == {3}


def test_superseded_and_rejected_slips_are_not_rounds(rounds):
    """Duplicates and rejected slips are not real deposits and must not be counted into a skewed round number"""
    from ocrslip.db import connect, deposit_rounds

    first, second, third = rounds(3)
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET review_status='rejected' WHERE id=%s",
                     (second,))
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET superseded_by=%s WHERE id=%s",
                     (first, third))
        conn.commit()
        got = deposit_rounds(conn, [_plate_norm()])

    assert got == {}, "one real round remains, so no badge is needed at all"


def test_rounds_of_other_plates_never_bleed_in(rounds):
    """Different cars must have their rounds counted separately, not pooled together"""
    from ocrslip.db import connect, deposit_rounds
    from ocrslip.normalize import norm_plate

    mine = rounds(2)
    other = rounds(2, plate="1กก1111")
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm(), norm_plate("1กก1111")])

    assert {got[i]["total"] for i in mine + other} == {2}
    assert [got[i]["round"] for i in other] == [1, 2]


def test_history_shows_every_round_with_its_slot_and_status(rounds):
    """The slip page must show which bay each round used and which round is still uncollected"""
    from ocrslip.db import connect, deposit_history

    ids = rounds(3)
    with connect() as conn:
        hist = deposit_history(conn, _plate_norm())

    assert [h["id"] for h in hist] == ids
    assert [h["round"] for h in hist] == [1, 2, 3]
    assert [h["car_status"] for h in hist] == ["returned", "returned", "stored"]
    assert hist[2]["location"] == "อาคาร 3 ชั้น 2"


def test_history_of_an_unknown_plate_is_empty(pgenv):
    """A slip whose plate could not be read (empty plate_norm) must not sweep up other equally empty slips"""
    from ocrslip.db import connect, deposit_history

    with connect() as conn:
        assert deposit_history(conn, "") == []


# ---------- the web pages ----------

def test_search_puts_the_round_still_parked_first(rounds, worker):
    """Searching a plate must put the still-parked round first, not an already-closed one.

    Every round scores identically (one set of plate, name and phone), so the order comes down to
    the secondary sort. Here the closed round is deliberately made the most recently recorded slip:
    ordered by record time alone, the collected slip would come first — exactly the slip exit staff
    must not open first.
    """
    from ocrslip.db import connect

    ids = rounds(3)
    with connect() as conn:  # the first round (already collected) is the last one recorded
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET created_at = now() WHERE id=%s",
                     (ids[0],))
        conn.commit()

    page = worker("staff").get(f"/api/search?q={PLATE}").text

    # Ordered by position on the page, not by the order the slips were created
    order = sorted((i for i in ids if f"/slips/{i}" in page), key=lambda i: page.index(i))
    assert order[0] == ids[-1], "the still-parked round must come before the collected one"


def test_search_labels_which_round_each_row_is(rounds, worker):
    ids = rounds(3)
    page = worker("staff").get(f"/api/search?q={PLATE}").text

    assert "ฝากรอบที่ 3 จาก 3" in page and "ฝากรอบที่ 1 จาก 3" in page
    assert len(ids) == 3


def test_slip_page_lists_all_rounds_of_the_plate(rounds, worker):
    """The page where a car gets released must show every round right there, with no need to go back to search"""
    ids = rounds(3)
    page = worker("staff").get(f"/slips/{ids[1]}").text

    assert "ทะเบียนนี้ฝากมาแล้ว 3 รอบ" in page
    assert "← ใบนี้" in page
    assert f"/slips/{ids[2]}" in page, "it must be possible to jump to another round"


def test_slip_page_of_a_single_round_has_no_history_card(rounds, worker):
    """A single-round slip needs no history card whose one row is itself"""
    only = rounds(1)
    page = worker("staff").get(f"/slips/{only[0]}").text

    assert "ฝากมาแล้ว" not in page


# ---------- the same slip uploaded repeatedly (one day) ----------

def test_slips_of_the_same_day_are_one_round_not_many(dup_day):
    """Three same-day duplicates must not become "round 1/2/3 of 3".

    Origin: one paper slip photographed three times, all three escaping the duplicate check at
    approval time (the parking spot transcribed differently, one of them empty, another already
    closed). The round badge then read as this car having parked three times, which is untrue and
    led exit staff to close the wrong slip.
    """
    from ocrslip.db import connect, deposit_rounds

    ids = dup_day(3)
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm()])

    assert [got[i]["round"] for i in ids] == [1, 1, 1]
    assert {got[i]["total"] for i in ids} == {1}, "one day means one round"
    assert {got[i]["dup"] for i in ids} == {3}, "it must convey that three slips share that day"


def test_search_calls_same_day_slips_duplicates_not_extra_rounds(dup_day, worker):
    ids = dup_day(3)
    page = worker("staff").get(f"/api/search?q={PLATE}").text

    assert "อาจเป็นใบซ้ำ · วันนี้มี 3 ใบ" in page
    assert "ฝากรอบที่" not in page, "a single round needs no round badge"
    assert len(ids) == 3


def test_a_real_second_round_still_counts_even_with_a_duplicate(dup_day):
    """Two real rounds with a duplicate in the first must still read "of 2 rounds", not 3"""
    from ocrslip.db import connect, deposit_rounds

    twins = dup_day(2, day="2026-09-10")
    later = dup_day(1, day="2026-09-20")[0]
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm()])

    assert [got[i]["round"] for i in twins] == [1, 1]
    assert got[later]["round"] == 2
    assert {got[i]["total"] for i in twins + [later]} == {2}
    assert got[later]["dup"] == 1, "the second round has one slip, so nothing should be flagged as duplicate"


def test_slips_without_a_deposit_date_are_not_duplicates_of_each_other(dup_day):
    """Every slip with an unreadable date shares the same "day" of NULL and must not all be lumped together as duplicates"""
    from ocrslip.db import connect, deposit_rounds

    ids = dup_day(2, day=None)
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm()])

    assert {got[i]["dup"] for i in ids} == {1}
    assert {got[i]["total"] for i in ids} == {2}, "indistinguishable means counting them as separate rounds for now"


def test_slip_page_marks_the_duplicate_rows(dup_day, worker):
    ids = dup_day(2)
    page = worker("staff").get(f"/slips/{ids[0]}").text

    assert "ทะเบียนนี้ฝากมาแล้ว 1 รอบ" in page and "(2 ใบ)" in page
    assert "ซ้ำ" in page
    assert f"/slips/{ids[1]}" in page, "it must be possible to jump to the other duplicate"


def _plate_norm() -> str:
    from ocrslip.normalize import norm_plate

    return norm_plate(PLATE)
