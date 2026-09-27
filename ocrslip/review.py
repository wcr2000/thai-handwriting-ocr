"""ตัดสินว่าใบไหนต้องให้คนตรวจ (human-in-the-loop)

OCR ไม่เขียนข้อมูลจริงทันที — ทุกใบเข้าคิว pending ก่อน
ใบที่เข้าเกณฑ์ด้านล่างจะถูกชู flag ให้คนตรวจก่อน ที่เหลือแค่กดยืนยันผ่านเร็ว
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
}

# ทะเบียนไทย: หมวดอักษร 1-3 ตัว (อาจมีเลขนำหน้าแบบใหม่ เช่น 5ขภ) + เลข 1-4 ตัว
PLATE_RE = re.compile(r"^\d?[ก-ฮ]{1,3}\d{1,4}$")


def low_confidence_fields(confidence: dict[str, float]) -> list[str]:
    return sorted(f for f, c in confidence.items() if c is not None and c < CONFIDENCE_THRESHOLD)


def field_problems(fields: dict[str, Any]) -> dict[str, str]:
    """คืน {field: เหตุผลภาษาไทย} ของช่องที่ผิดรูปแบบหรือหายไป"""
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
    """คืน (review_reason, field_problems) — ถ้า review_reason ว่าง แปลว่าผ่านเร็วได้"""
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
