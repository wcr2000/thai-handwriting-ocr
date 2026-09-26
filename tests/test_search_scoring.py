"""ทดสอบการให้คะแนนความใกล้เคียง (ไม่แตะฐานข้อมูล)

ข้อมูลในไฟล์นี้เป็นค่าสมมติทั้งหมด (repo เปิดสาธารณะ)
"""

import pytest

from ocrslip.search import _score, classify

ROW = {"name_norm": "สมชาย ใจดี", "tel_digits": "0800000000",
       "plate_norm": "กก1234", "brand_norm": "toyota"}


@pytest.mark.parametrize("q,kind", [
    ("0800000000", "tel"), ("080-000-0000", "tel"),
    ("กก1234", "plate"), ("สมชาย", "name"), ("สมหญิง รักไทย", "name"),
])
def test_classify(q, kind):
    assert classify(q) == kind


def test_exact_phone_is_full_score():
    score, why = _score(ROW, "tel", "", "0800000000", "0800000000")
    assert score == 100.0 and why == "เบอร์โทร"


def test_one_digit_off_still_high():
    score, _ = _score(ROW, "tel", "", "0800000001", "0800000001")
    assert score >= 85


def test_swapped_name_order_matches():
    score, why = _score(ROW, "name", "ใจดี สมชาย", "", "")
    assert score >= 95 and why == "ชื่อ"


def test_partial_name_matches():
    score, _ = _score(ROW, "name", "สมช", "", "")
    assert score >= 90


def test_unrelated_query_scores_low():
    score, _ = _score(ROW, "name", "วิไลวรรณ ศรีสุข", "", "")
    assert score < 60
