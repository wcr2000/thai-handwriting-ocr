"""Decide which slips need a human reviewer (human-in-the-loop).

OCR never writes straight into the real data — every slip lands in the pending queue
first. Slips meeting the criteria below are flagged for a reviewer's attention; the rest
only need a quick confirm-and-pass.
"""

from __future__ import annotations

import re
from typing import Any

from .config import CONFIDENCE_THRESHOLD
from .normalize import norm_phone, norm_plate, parse_date

REQUIRED = ("name", "tel", "noplate")

REASON_LABELS = {
    "low_confidence": "model ไม่มั่นใจในบางช่อง",
    "missing_field": "มีช่องสำคัญที่อ่านไม่ออก",
    "format_invalid": "รูปแบบข้อมูลไม่ถูกต้อง",
    "duplicate_suspect": "อาจซ้ำกับใบที่ยังจอดอยู่",
    "duplicate_image": "รูปนี้เคยอัปโหลดแล้ว",
    # Set by hand, never by evaluate(). Used to pull slips confirmed back when cropping
    # was still broken into the queue again: the reviewer at the time was looking at a
    # skewed or clipped image, so they may have typed from the wrong slip.
    "recheck_bad_crop": "ตรวจซ้ำ — ยืนยันไว้ตอนที่ระบบยัง crop รูปพลาด",
}

# Thai plates: a 1-3 character letter group (optionally with the newer leading digit, as
# in 5ขภ) followed by 1-4 digits
PLATE_RE = re.compile(r"^\d?[ก-ฮ]{1,3}\d{1,4}$")


def low_confidence_fields(confidence: dict[str, float]) -> list[str]:
    return sorted(f for f, c in confidence.items() if c is not None and c < CONFIDENCE_THRESHOLD)


def field_problems(fields: dict[str, Any]) -> dict[str, str]:
    """Return {field: reason} for fields that are malformed or missing. Reasons are Thai: staff read them."""
    problems: dict[str, str] = {}

    for f in REQUIRED:
        if not (fields.get(f) or "").strip() if isinstance(fields.get(f), str) else not fields.get(f):
            problems[f] = "อ่านไม่ออก / ไม่ได้กรอก"

    tel = norm_phone(fields.get("tel"))
    if tel and (len(tel) != 10 or not tel.startswith("0")):
        problems["tel"] = f"เบอร์ได้ {len(tel)} หลัก (ต้องเป็น 10 หลักขึ้นต้นด้วย 0)"

    plate = norm_plate(fields.get("noplate"))
    if plate and not PLATE_RE.match(plate):
        problems["noplate"] = "รูปแบบทะเบียนไม่ปกติ"

    if fields.get("date") and parse_date(fields.get("date")) is None:
        problems["date"] = "อ่านวันที่ไม่ออก"

    return problems


def evaluate(
    fields: dict[str, Any], confidence: dict[str, float], duplicates: int = 0
) -> tuple[list[str], dict[str, str]]:
    """Return (review_reason, field_problems). An empty review_reason means it can fast-pass."""
    problems = field_problems(fields)
    low = low_confidence_fields(confidence)

    reasons: list[str] = []
    if any(problems.get(f) == "อ่านไม่ออก / ไม่ได้กรอก" for f in REQUIRED):
        reasons.append("missing_field")
    if any(f not in REQUIRED or problems[f] != "อ่านไม่ออก / ไม่ได้กรอก" for f in problems):
        reasons.append("format_invalid")
    if low:
        reasons.append("low_confidence")
    if duplicates:
        reasons.append("duplicate_suspect")

    for f in low:
        problems.setdefault(f, "model ไม่มั่นใจ")
    return sorted(set(reasons)), problems
