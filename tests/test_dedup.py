"""Duplicates: re-uploading the same photo must not create a new slip, and the queue must not re-serve a reviewed slip.

Origin: the upload page did not clear the file field after a successful upload, so anyone who
thought it had not gone through pressed again. In production this produced 650 surplus slips,
and reviewers kept being handed slips a colleague had already done — as though the work had not
been saved.

Needs a real Postgres: what is under test is the conditions inside the queue's UPDATE/SELECT and
the FK ON DELETE SET NULL, neither of which can be mocked. See tests/conftest.py for how to run
it.
"""

import pytest

from conftest import TEST_SCHEMA, needs_db
from test_preprocess import WOOD, fake_photo

pytestmark = needs_db


@pytest.fixture
def photo():
    """A synthetic photo of one slip (identical bytes every time, hence an identical hash, modelling a repeat upload)"""
    from ocrslip.imageio import encode_jpeg, to_pil

    return encode_jpeg(to_pil(fake_photo(WOOD)), quality=90)


@pytest.fixture
def fake_ocr(monkeypatch):
    """Stand in for read_slip so the tests never call a real model (costly and slow), counting the calls made"""
    from ocrslip.ocr import OcrResult
    from ocrslip.web import pipeline

    calls = []

    def _read(jpeg, model="fake/model"):
        calls.append(jpeg)
        return OcrResult(
            model="fake/model", latency_s=0.1,
            fields={"name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
                    "province": "กรุงเทพมหานคร", "brand": "รีโว่", "typecar": "เก๋ง",
                    "location": "อาคาร 1 ชั้น 2", "date": "2026-09-28"},
            confidence={"name": 0.95, "tel": 0.95, "noplate": 0.95},
        )

    monkeypatch.setattr(pipeline, "read_slip", _read)
    return calls


def rows(sql: str, params=()) -> list[dict]:
    from ocrslip.db import connect

    with connect() as conn:
        return conn.execute(sql.replace("{s}", TEST_SCHEMA), params).fetchall()


def clear():
    from ocrslip.db import connect

    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.commit()


# ---------- the duplicate-upload gate (before any model call) ----------

def test_uploading_the_same_photo_twice_keeps_one_slip(pgenv, photo, fake_ocr):
    """The heart of the fix: the same photo resubmitted must yield no new slip and no second OCR charge"""
    from ocrslip.db import connect
    from ocrslip.web.pipeline import ingest

    clear()
    with connect() as conn:
        first = ingest(conn, photo, uploaded_by="ก")
    assert first["ok"] and not first.get("duplicate_of")

    with connect() as conn:
        second = ingest(conn, photo, uploaded_by="ข")

    assert second["ok"]
    assert second["duplicate_of"], "a resubmitted photo must be detected"
    assert second["id"] == first["id"], "it must point back at the existing slip, not create a new one"
    assert len(rows("SELECT id FROM {s}.slips")) == 1
    assert len(fake_ocr) == 1, "the second attempt must not call the model at all"


def test_duplicate_upload_says_which_slip_it_matched(pgenv, photo, fake_ocr):
    """Whoever re-uploaded has to be told what state the existing slip is in, not merely that it is a duplicate"""
    from ocrslip.db import connect
    from ocrslip.web.pipeline import ingest

    clear()
    with connect() as conn:
        ingest(conn, photo, uploaded_by="ก")
        twin = ingest(conn, photo, uploaded_by="ข")["duplicate_of"]

    assert twin["review_status"] == "pending"
    assert twin["uploaded_by"] == "ก", "it must name whoever uploaded the original, not whoever is re-uploading"


# ---------- pulling duplicates out of the queue at approval time ----------

TWIN = {"name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
        "province": "กรุงเทพมหานคร", "brand": "รีโว่", "typecar": "เก๋ง",
        "location": "อาคาร 1 ชั้น 2", "date": "2026-09-28"}


@pytest.fixture
def twins(pgenv):
    """Create N pending slips that are the same slip (matching plate, phone and deposit date), oldest first"""
    from ocrslip.db import connect
    from ocrslip.normalize import norm_phone, norm_plate

    def _make(n: int = 2, date: str = "2026-09-28") -> list[str]:
        with connect() as conn:
            ids = [
                conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slips
                        (name, tel, tel_digits, plate_raw, plate_norm, deposit_date, location,
                         review_status, needs_review, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', true,
                                now() - (%s || ' min')::interval)
                        RETURNING id::text""",
                    (TWIN["name"], TWIN["tel"], norm_phone(TWIN["tel"]), TWIN["noplate"],
                     norm_plate(TWIN["noplate"]), date, TWIN["location"], 100 - i),
                ).fetchone()["id"]
                for i in range(n)
            ]
            conn.commit()
        return ids

    clear()
    return _make


def approve(client, slip_id: str, **over):
    return client.post(f"/review/{slip_id}/approve",
                       data={**TWIN, "reviewed_by": "krit", **over},
                       follow_redirects=False)


def superseded_by(slip_id: str):
    row = rows("SELECT superseded_by::text AS s FROM {s}.slips WHERE id = %s", (slip_id,))[0]
    return row["s"]


def test_approving_one_slip_pulls_its_twin_out_of_the_queue(twins, worker):
    """What reviewers hit in practice: once the first slip is reviewed, its twin must not be served to anybody"""
    first, second = twins(2)
    client = worker("staff")

    assert approve(client, first).status_code == 303
    assert superseded_by(second) == first

    # The queue is now empty: /review/next must have nothing left to claim
    r = client.get("/review/next", follow_redirects=False)
    assert r.headers["location"] == "/review", "the queue must not serve a duplicate for review again"


def test_superseded_slip_is_visible_in_its_own_pile(twins, worker):
    """A duplicate must not vanish silently — when something is wrongly flagged, there has to be somewhere to find it"""
    first, second = twins(2)
    client = worker("staff")
    approve(client, first)

    page = client.get("/review?filter=dup").text
    assert "ซ้ำกับใบที่ตรวจแล้ว" in page
    counts = rows(
        "SELECT count(*) FILTER (WHERE superseded_by IS NOT NULL) AS dup,"
        " count(*) FILTER (WHERE review_status='pending' AND superseded_by IS NULL) AS todo"
        " FROM {s}.slips")[0]
    assert (counts["dup"], counts["todo"]) == (1, 0)


def test_a_whole_pile_of_twins_collapses_in_one_approval(twins, worker):
    """Production saw up to 9 slips per group — one approval has to clear the entire rest of the pile"""
    ids = twins(5)
    client = worker("staff")
    approve(client, ids[0])

    assert all(superseded_by(i) == ids[0] for i in ids[1:])


def test_same_car_deposited_on_another_day_stays_in_the_queue(twins, worker):
    """The same car parked again is a fresh deposit and must not be pulled from the queue"""
    from ocrslip.db import connect

    first, second = twins(2)
    with connect() as conn:  # the second slip is next month's deposit
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET deposit_date='2026-10-28' WHERE id=%s",
                     (second,))
        conn.commit()

    client = worker("staff")
    approve(client, first)
    assert superseded_by(second) is None


def test_second_round_on_the_same_day_stays_in_the_queue(twins, worker):
    """Parked in the morning, collected at midday, parked again that evening — the evening slip must survive approving the morning one.

    Here the deposit date cannot help (both agree); what separates them is the parking spot, since
    a new round gets a new bay. Allowed to be flagged as a duplicate, the evening car is genuinely
    parked with no active slip.
    """
    from ocrslip.db import connect

    first, second = twins(2)
    with connect() as conn:  # the second slip is the evening round, in a different bay
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET location='อาคาร 3 ชั้น 5 c2' WHERE id=%s",
                     (second,))
        conn.commit()

    approve(worker("staff"), first)
    assert superseded_by(second) is None


def test_twin_of_a_car_already_returned_stays_in_the_queue(twins, worker):
    """A returned slip has closed its own round, so the next one is a new round, even in the same bay"""
    from ocrslip.db import connect

    first, second = twins(2)
    with connect() as conn:  # the first round's car has already been collected
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET car_status='returned',"
                     f" returned_at=now() WHERE id=%s", (first,))
        conn.commit()

    approve(worker("staff"), first)
    assert superseded_by(second) is None


def test_same_photo_still_collapses_even_after_the_car_went_home(pgenv, photo, fake_ocr):
    """A byte-identical file is unambiguously a duplicate; the new-round conditions must not interfere with the image-hash branch"""
    from ocrslip.db import connect
    from ocrslip.web.pipeline import ingest

    clear()
    with connect() as conn:
        first = ingest(conn, photo, uploaded_by="ก")["id"]
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET car_status='returned' WHERE id=%s",
                     (first,))
        # The second slip holds the same image (modelling what was in flight before the gate was added to ingest)
        second = conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slips (plate_raw, review_status, needs_review)
                VALUES ('กก1234', 'pending', true) RETURNING id::text""").fetchone()["id"]
        conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slip_images (slip_id, kind, sha256, bytes)
                SELECT %s, kind, sha256, bytes FROM {TEST_SCHEMA}.slip_images
                 WHERE slip_id = %s AND kind = 'original'""", (second, first))
        from ocrslip.db import mark_superseded
        assert mark_superseded(conn, first) == 1
        conn.commit()

    assert superseded_by(second) == first


def test_rejected_twin_is_left_alone(twins, worker):
    """A slip somebody rejected is work a human did; no script may bury it with an inference"""
    first, second = twins(2)
    client = worker("staff")
    client.post(f"/review/{second}/reject", data={"reason": "รูปเบลอ", "reviewed_by": "krit"},
                follow_redirects=False)
    approve(client, first)

    row = rows("SELECT review_status, superseded_by FROM {s}.slips WHERE id=%s", (second,))[0]
    assert (row["review_status"], row["superseded_by"]) == ("rejected", None)


def test_deleting_the_real_slip_puts_its_twin_back_in_the_queue(twins, worker):
    """If the canonical slip is deleted, its duplicate must return to the queue rather than both vanishing (FK ON DELETE SET NULL)"""
    from ocrslip.db import connect, delete_slip

    first, second = twins(2)
    approve(worker("staff"), first)
    assert superseded_by(second) == first

    with connect() as conn:
        delete_slip(conn, first)
        conn.commit()
    assert superseded_by(second) is None


# ---------- the cleanup script for pre-existing data ----------

def test_dedup_apply_clears_twins_of_already_approved_slips(twins, worker):
    """What was already in flight before the fix: duplicates with an approved sibling must be clearable in one pass"""
    from ocrslip.db import connect
    from ocrslip.dedup import group_duplicates, keeper_of, load_slips
    from ocrslip.dedup import mark_superseded

    first, second, third = twins(3)
    # Model the pre-fix state: the first slip approved, the rest still queued (never went through
    # the approval hook)
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET review_status='approved',"
                     f" needs_review=false, reviewed_by='krit', reviewed_at=now()"
                     f" WHERE id=%s", (first,))
        conn.commit()

    with connect() as conn:
        groups = group_duplicates(load_slips(conn))
        assert len(groups) == 1 and len(groups[0]) == 3
        assert keeper_of(groups[0])["id"] == first
        assert mark_superseded(conn, first) == 2
        conn.commit()

    assert superseded_by(second) == first and superseded_by(third) == first
