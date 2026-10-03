"""Tests for the criteria routing slips into the review queue — an incomplete slip must never slip through silently.

Every value in this file is fabricated (this repository is public).
"""

from ocrslip.review import evaluate, field_problems

GOOD = {"name": "สมชาย ใจดี", "tel": "0800000000", "noplate": "กก1234", "date": "26/9/69"}
CONF = {"name": 0.95, "tel": 0.99, "noplate": 0.93, "date": 0.95}


def test_clean_slip_passes():
    reasons, problems = evaluate(GOOD, CONF)
    assert reasons == [] and problems == {}


def test_short_phone_is_flagged():
    reasons, problems = evaluate({**GOOD, "tel": "08123"}, CONF)
    assert "format_invalid" in reasons
    assert "tel" in problems


def test_missing_required_field():
    reasons, problems = evaluate({**GOOD, "name": None}, CONF)
    assert "missing_field" in reasons


def test_low_confidence_flagged():
    reasons, _ = evaluate(GOOD, {**CONF, "name": 0.4})
    assert "low_confidence" in reasons


def test_duplicate_flagged():
    reasons, _ = evaluate(GOOD, CONF, duplicates=1)
    assert "duplicate_suspect" in reasons


def test_plate_without_letters_is_only_a_warning():
    """Real slips exist with digits only in that field — warn, but do not treat the field as empty"""
    problems = field_problems({**GOOD, "noplate": "1234"})
    assert "noplate" in problems


def test_conflicts_flags_only_when_every_view_agrees_and_contradicts_stored():
    """Only pull a slip back for a second look when the two views agree with each other and conflict with what is stored.

    Two views disagreeing means the handwriting is ambiguous, not that the human-confirmed value
    is wrong — and in that case a reviewer must not be bothered again.
    """
    from ocrslip.recheck import conflicts

    stored = {"name": "สมชาย ใจดี", "tel": "0812345678", "noplate": "กก1234"}

    # Both views read the same plate, and a different car from the stored one -> pull it back
    same = {"name": "สมชาย ใจดี", "tel": "0812345678", "noplate": "5ขค9999"}
    assert conflicts(stored, [same, dict(same)]) == ["noplate"]

    # The two views disagree -> ambiguous handwriting, not evidence the stored value is wrong
    a = {**stored, "noplate": "5ขค9999"}
    b = {**stored, "noplate": "2งจ1111"}
    assert conflicts(stored, [a, b]) == []

    # The reads match what is already stored -> nothing to do
    assert conflicts(stored, [dict(stored), dict(stored)]) == []

    # A field still empty in the system does not count as a conflict
    assert conflicts({**stored, "noplate": None}, [same, dict(same)]) == []
