"""ทดสอบเกณฑ์คัดใบเข้าคิวตรวจ — ใบที่ข้อมูลไม่ครบต้องไม่หลุดเข้าระบบเงียบ ๆ

ข้อมูลในไฟล์นี้เป็นค่าสมมติทั้งหมด (repo เปิดสาธารณะ)
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
    """ของจริงมีใบที่เขียนแต่ตัวเลข — ต้องเตือน แต่ไม่ถือว่าช่องนั้นว่าง"""
    problems = field_problems({**GOOD, "noplate": "1234"})
    assert "noplate" in problems


def test_conflicts_flags_only_when_every_view_agrees_and_contradicts_stored():
    """หยิบใบมาให้คนดูซ้ำเฉพาะตอนที่ภาพคนละมุมอ่านได้ตรงกันและขัดกับที่เก็บไว้

    ถ้าสองมุมอ่านได้ไม่ตรงกัน แปลว่าลายมือกำกวม ไม่ใช่หลักฐานว่าค่าที่คนยืนยันผิด
    — กรณีนั้นห้ามไปรบกวนคนตรวจซ้ำ
    """
    from ocrslip.recheck import conflicts

    stored = {"name": "สมชาย ใจดี", "tel": "0812345678", "noplate": "กก1234"}

    # ทั้งสองมุมอ่านได้ทะเบียนเดียวกัน และคนละคันกับที่เก็บไว้ -> ควรดึงกลับ
    same = {"name": "สมชาย ใจดี", "tel": "0812345678", "noplate": "5ขค9999"}
    assert conflicts(stored, [same, dict(same)]) == ["noplate"]

    # สองมุมอ่านได้ไม่ตรงกัน -> ลายมือกำกวม ไม่ใช่หลักฐานว่าค่าที่เก็บไว้ผิด
    a = {**stored, "noplate": "5ขค9999"}
    b = {**stored, "noplate": "2งจ1111"}
    assert conflicts(stored, [a, b]) == []

    # อ่านได้ตรงกับที่เก็บไว้อยู่แล้ว -> ไม่มีอะไรต้องทำ
    assert conflicts(stored, [dict(stored), dict(stored)]) == []

    # ช่องที่ยังว่างในระบบ ไม่ถือว่าขัดแย้ง
    assert conflicts({**stored, "noplate": None}, [same, dict(same)]) == []
