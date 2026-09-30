"""ทดสอบ normalize — ถ้าตรงนี้พัง search จะพังเงียบ ๆ โดยไม่มีใครรู้

ข้อมูลในไฟล์นี้เป็นค่าสมมติทั้งหมด ห้ามใช้ข้อมูลจากใบฝากรถจริงมาเป็น fixture
เพราะ repo นี้เปิดสาธารณะ
"""

import datetime as dt

import pytest

from ocrslip.normalize import (
    norm_brand, norm_date, norm_name, norm_phone, norm_plate, norm_province, parse_date, split_province,
)


@pytest.mark.parametrize("raw,want", [
    ("26/9/69", "2026-09-26"),          # พ.ศ. ย่อ 2 หลัก
    ("26/09/2569", "2026-09-26"),       # พ.ศ. เต็ม
    ("26/09/2026", "2026-09-26"),       # ค.ศ. เต็ม
    ("26 ก.ย. 69", "2026-09-26"),       # เดือนย่อภาษาไทย
    ("26 กันยายน 2569", "2026-09-26"),  # เดือนเต็ม
    ("๒๖/๐๙/๒๕๖๙", "2026-09-26"),       # เลขไทย
    ("26", ""),                          # เขียนแค่วันที่ ไม่พอจะรู้เดือน
    ("", ""),
])
def test_date_formats(raw, want):
    assert norm_date(raw) == want


def test_date_invalid_returns_none():
    assert parse_date("32/13/69") is None


def test_date_real_value():
    assert parse_date("26 ก.ย. 2569") == dt.date(2026, 9, 26)


@pytest.mark.parametrize("raw,want", [
    ("26/9/26", dt.date(2026, 9, 26)),      # ค.ศ. ย่อ — เคยกลายเป็น 2083
    ("26/9/69", dt.date(2026, 9, 26)),      # พ.ศ. ย่อ ยังต้องได้ปีเดิม
    ("26/09/2569", dt.date(2026, 9, 26)),
    ("26/09/2026", dt.date(2026, 9, 26)),
])
def test_two_digit_year_takes_the_reading_closest_to_today(raw, want):
    """ปีที่คนเขียนบนใบจอดรถคือปีนี้ ไม่ใช่ปีที่ห่างไป 57 ปี

    กติกาเดิมบวก 2600 ให้ปี 2 หลักที่น้อยกว่า 50 แล้วถือเป็น พ.ศ. ลบ 543 —
    "26" ที่หมายถึง ค.ศ. 2026 จึงกลายเป็น 2083 เจอในฐานข้อมูลจริงกว่า 300 ใบ
    และทำให้ใบเดียวกันที่ถ่ายซ้ำถูกนับเป็นการฝากคนละรอบเพราะวันที่ไม่ตรงกัน
    """
    assert parse_date(raw) == want


def test_a_day_that_does_not_exist_in_the_closest_year_is_not_pushed_to_another_year():
    """29/2/68 -> พ.ศ. 2568 = 2025 ซึ่งไม่มี 29 ก.พ. ต้องได้ None ไม่ใช่ 2068

    ยอมรับว่าอ่านวันที่ไม่ออกดีกว่าเดาปีที่ห่างไป 40 ปี — ใบที่วันที่ว่างมีคนตรวจแก้ได้
    แต่ใบที่ขึ้นปี 2068 ไม่มีใครจับได้ว่าผิด
    """
    assert parse_date("29/2/68") is None
    assert parse_date("29/2/67") == dt.date(2024, 2, 29), "ปีที่มีวันนั้นจริงต้องอ่านได้"


@pytest.mark.parametrize("raw,want", [
    ("080-000-0000", "0800000000"),
    ("080 000 0000", "0800000000"),
    ("๐๘๐๐๐๐๐๐๐๐", "0800000000"),
    ("+66800000000", "0800000000"),
])
def test_phone(raw, want):
    assert norm_phone(raw) == want


@pytest.mark.parametrize("raw,plate,prov", [
    ("1กก 1234 กรุงเทพ", "1กก1234", "กรุงเทพ"),
    ("ขข 9999 อยุธยา", "ขข9999", "อยุธยา"),
    ("กก-1234", "กก1234", None),
])
def test_plate_and_province(raw, plate, prov):
    assert norm_plate(raw) == plate
    assert split_province(raw)[1] == prov


def test_province_variants_collapse():
    assert norm_province("กทม.") == norm_province("กรุงเทพมหานคร") == "กรุงเทพ"


@pytest.mark.parametrize("raw,want", [
    ("โตโยต้ายาริสสีขาว", "toyota"),
    ("TOYOTA VIOS", "toyota"),
    ("IZUZU", "isuzu"),
    ("อีซูซุ", "isuzu"),
    ("Honda City", "honda"),
])
def test_brand_canonical(raw, want):
    assert norm_brand(raw) == want


def test_name_strips_title():
    assert norm_name("นาย สมชาย  ใจดี") == "สมชาย ใจดี"
    assert norm_name("น.ส.สมหญิง รักไทย") == "สมหญิง รักไทย"
