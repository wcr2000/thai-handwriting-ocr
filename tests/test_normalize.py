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
