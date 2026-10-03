"""Normalization tests — break this and search fails silently, with nobody the wiser.

Every value in this file is fabricated. Never use data from a real parking slip as a fixture:
this repository is public.
"""

import datetime as dt

import pytest

from ocrslip.normalize import (
    norm_brand, norm_date, norm_name, norm_phone, norm_plate, norm_province, parse_date, split_province,
)


@pytest.mark.parametrize("raw,want", [
    ("26/9/69", "2026-09-26"),          # two-digit Buddhist Era year
    ("26/09/2569", "2026-09-26"),       # full Buddhist Era year
    ("26/09/2026", "2026-09-26"),       # full Gregorian year
    ("26 ก.ย. 69", "2026-09-26"),       # abbreviated Thai month
    ("26 กันยายน 2569", "2026-09-26"),  # full Thai month name
    ("๒๖/๐๙/๒๕๖๙", "2026-09-26"),       # Thai numerals
    ("26", ""),                          # day alone: not enough to infer the month
    ("", ""),
])
def test_date_formats(raw, want):
    assert norm_date(raw) == want


def test_date_invalid_returns_none():
    assert parse_date("32/13/69") is None


def test_date_real_value():
    assert parse_date("26 ก.ย. 2569") == dt.date(2026, 9, 26)


@pytest.mark.parametrize("raw,want", [
    ("26/9/26", dt.date(2026, 9, 26)),      # two-digit Gregorian — used to become 2083
    ("26/9/69", dt.date(2026, 9, 26)),      # two-digit Buddhist Era must still give the same year
    ("26/09/2569", dt.date(2026, 9, 26)),
    ("26/09/2026", dt.date(2026, 9, 26)),
])
def test_two_digit_year_takes_the_reading_closest_to_today(raw, want):
    """The year somebody writes on a parking slip is this year, not one 57 years away.

    The old rule added 2600 to any two-digit year below 50 and then treated it as Buddhist Era,
    subtracting 543 — so a "26" meaning 2026 CE became 2083. Found on more than 300 slips in the
    real database, and it made re-photographed copies of one slip count as separate deposits,
    because their dates disagreed.
    """
    assert parse_date(raw) == want


def test_a_day_that_does_not_exist_in_the_closest_year_is_not_pushed_to_another_year():
    """29/2/68 -> BE 2568 = 2025, which has no 29 Feb, so the answer must be None rather than 2068.

    Admitting the date is unreadable beats guessing a year 40 years out: a reviewer can fix a slip
    with an empty date, but nobody will ever catch a slip showing the year 2068.
    """
    assert parse_date("29/2/68") is None
    assert parse_date("29/2/67") == dt.date(2024, 2, 29), "a year where that date does exist must parse"


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
