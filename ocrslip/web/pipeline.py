"""รวมขั้นตอน: ไฟล์ที่อัปโหลด -> preprocess -> OCR -> ตัดสินว่าต้องตรวจไหม -> บันทึกลง DB"""

from __future__ import annotations

from typing import Any

import psycopg

from ..config import DB_SCHEMA, OCR_MODEL
from ..db import add_image, image_seen, insert_slip
from ..imageio import encode_jpeg
from ..normalize import norm_phone, norm_plate
from ..ocr import read_slip
from ..preprocess import preprocess, upright
from ..review import evaluate

# bench ชี้ว่า crop อย่างเดียวแม่นกว่าการปรับสี (71% vs 68%) — การเพิ่ม contrast ทำให้เส้นปากกาบางเสียรูป
VARIANT = "v1_crop"


def count_duplicates(conn: psycopg.Connection, fields: dict[str, Any]) -> int:
    """นับใบที่ยังฝากอยู่และมีทะเบียนหรือเบอร์ตรงกัน — กันคีย์ซ้ำ/รถคันเดิมฝากซ้ำ"""
    plate, tel = norm_plate(fields.get("noplate")), norm_phone(fields.get("tel"))
    if not plate and not tel:
        return 0
    cur = conn.execute(
        f"""SELECT count(*) AS n FROM {DB_SCHEMA}.slips
            WHERE car_status = 'stored' AND review_status <> 'rejected'
              AND ((%s <> '' AND plate_norm = %s) OR (%s <> '' AND tel_digits = %s))""",
        (plate, plate, tel, tel),
    )
    return cur.fetchone()["n"]


def ingest(
    conn: psycopg.Connection,
    raw: bytes,
    *,
    created_by: str | None = None,
    uploaded_by: str | None = None,
    photographer: str | None = None,
) -> dict[str, Any]:
    """ประมวลผลรูป 1 ใบแล้วบันทึกเป็น pending คืนสรุปไว้แสดงผล"""
    pre = preprocess(raw)
    processed_jpeg = encode_jpeg(pre.cropped)
    original_jpeg = encode_jpeg(pre.raw, quality=85)

    res = read_slip(processed_jpeg, OCR_MODEL)
    if not res.ok:
        return {"ok": False, "error": res.error}

    # model อ่านใบกลับหัวได้อยู่แล้ว แต่คนตรวจอ่านไม่ได้ จึงเก็บรูปที่หมุนกลับมาตรงแล้ว
    # หมุนหลัง OCR ไม่ใช่ก่อน จะได้ไม่ต้องยิง model ซ้ำ
    cropped = upright(pre.cropped, res.orientation)
    if cropped is not pre.cropped:
        processed_jpeg = encode_jpeg(cropped)

    fields = {k: v for k, v in res.fields.items()}
    reasons, problems = evaluate(fields, res.confidence, count_duplicates(conn, fields))
    if image_seen(conn, processed_jpeg):
        reasons = sorted({*reasons, "duplicate_image"})

    slip_id = insert_slip(
        conn, fields,
        confidence=res.confidence,
        raw_ocr={"fields": res.fields, "quad_found": pre.quad_found, "problems": problems},
        review_reason=reasons,
        ocr_model=res.model,
        ocr_variant=VARIANT,
        usage=res.usage,
        latency_s=res.latency_s,
        created_by=created_by,
        uploaded_by=uploaded_by,
        photographer=photographer,
    )
    add_image(conn, slip_id, "processed", processed_jpeg, cropped.size)
    add_image(conn, slip_id, "original", original_jpeg, pre.raw.size)
    conn.commit()

    return {
        "ok": True, "id": slip_id, "fields": fields, "reasons": reasons,
        "problems": problems, "latency_s": res.latency_s,
    }
