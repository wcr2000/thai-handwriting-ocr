"""รวมขั้นตอน: ไฟล์ที่อัปโหลด -> preprocess -> OCR -> ตัดสินว่าต้องตรวจไหม -> บันทึกลง DB"""

from __future__ import annotations

from typing import Any

import psycopg
from PIL import Image

from ..config import DB_SCHEMA, OCR_MODEL
from ..db import add_image, image_seen, insert_slip
from ..imageio import encode_jpeg
from ..normalize import norm_phone, norm_plate
from ..ocr import OcrResult, read_slip
from ..preprocess import PreprocessResult, preprocess, upright
from ..review import evaluate

# bench ชี้ว่า crop อย่างเดียวแม่นกว่าการปรับสี (71% vs 68%) — การเพิ่ม contrast ทำให้เส้นปากกาบางเสียรูป
VARIANT = "v1_crop"


def build_raw_ocr(
    res: OcrResult, quad_found: bool, problems: dict[str, str], **extra: Any
) -> dict[str, Any]:
    """ร่องรอยของ OCR ที่เก็บไว้ตรวจย้อนหลัง — ใช้ร่วมกันระหว่างตอนอัปโหลดกับตอน reprocess

    orientation ต้องอยู่ในนี้เสมอ เพราะ reprocess ใช้มันตัดสินว่าใบไหน "ยังไม่เคยเช็ค
    ว่ากลับหัว" ถ้าไม่บันทึก ใบที่เพิ่งอัปโหลดจะถูกหยิบไปยิง model ซ้ำทุกครั้งที่รัน
    """
    return {
        "fields": res.fields,
        "quad_found": quad_found,
        "problems": problems,
        "orientation": res.orientation,
        **extra,
    }


def read_with_fallback(
    pre: PreprocessResult, model: str = OCR_MODEL
) -> tuple[OcrResult, Image.Image, bool]:
    """อ่านจากภาพที่ crop แล้ว ถ้าอ่านไม่ได้เลยค่อยลองใหม่ด้วยภาพเต็ม

    คืน (ผลที่ใช้, ภาพที่ใช้อ่าน, ใช้ภาพเต็มไหม)

    ตัวจับขอบกระดาษไม่มีทางถูก 100% — ที่เจอมาแล้วคือไปจับปึกกระดาษเปล่าข้าง ๆ
    และไปจับลายไม้บนโต๊ะ พอ crop ผิด model ก็คืน null ทุกช่องโดยไม่มีใครรู้ว่าเพราะอะไร
    การลองใหม่ด้วยภาพเต็มกู้เคสพวกนี้ได้หมดในคราวเดียว ไม่ต้องไล่จูน CV ทีละเคส
    และเสียค่าใช้จ่ายเพิ่มเฉพาะตอนที่พังจริง ๆ เท่านั้น
    """
    res = read_slip(encode_jpeg(pre.cropped), model)
    if not res.ok or not pre.quad_found or _read_something(res):
        return res, pre.cropped, False

    retry = read_slip(encode_jpeg(pre.raw), model)
    if retry.ok and _read_something(retry):
        return retry, pre.raw, True
    return res, pre.cropped, False


def _read_something(res: OcrResult) -> bool:
    """model อ่านอะไรออกมาได้บ้างไหม — ถ้าช่องสำคัญว่างหมด แปลว่าภาพที่ส่งไปใช้ไม่ได้"""
    return any((res.fields.get(f) or "") for f in ("name", "tel", "noplate"))


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
    original_jpeg = encode_jpeg(pre.raw, quality=85)

    res, used, full_frame = read_with_fallback(pre)
    if not res.ok:
        return {"ok": False, "error": res.error}

    # model อ่านใบกลับหัวได้อยู่แล้ว แต่คนตรวจอ่านไม่ได้ จึงเก็บรูปที่หมุนกลับมาตรงแล้ว
    # หมุนหลัง OCR ไม่ใช่ก่อน จะได้ไม่ต้องยิง model ซ้ำ
    cropped = upright(used, res.orientation)
    processed_jpeg = encode_jpeg(cropped)

    fields = {k: v for k, v in res.fields.items()}
    reasons, problems = evaluate(fields, res.confidence, count_duplicates(conn, fields))
    if image_seen(conn, processed_jpeg):
        reasons = sorted({*reasons, "duplicate_image"})

    slip_id = insert_slip(
        conn, fields,
        confidence=res.confidence,
        raw_ocr=build_raw_ocr(res, pre.quad_found and not full_frame, problems),
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
