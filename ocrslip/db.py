"""เชื่อมต่อ Postgres + คำสั่งที่เว็บใช้จริง

รันสร้าง schema:  python -m ocrslip.db init
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

from .config import APP_TIMEZONE, DATABASE_URL, DB_SCHEMA, REVIEW_CLAIM_MINUTES
from .normalize import (
    _base, norm_brand, norm_cartype, norm_name, norm_phone, norm_plate, norm_province, parse_date,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / "db" / "schema.sql"


def connect() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("ยังไม่ได้ตั้ง DATABASE_URL ใน .env")
    # -c TimeZone ต้องอยู่ตรงนี้ ไม่ใช่ไปแปลงตอน render — now() ที่เขียนลง returned_at/
    # reviewed_at และ interval ของการจองใบ ล้วนอ้างเขตเวลาของ session นี้ ถ้าแปลงทีหลัง
    # เฉพาะที่หน้าจอ จะมีที่ตกหล่นเสมอ (เคสที่เจอ: ใบปิดเวลา 16:15 แต่หน้าใบโชว์ 09:15)
    return psycopg.connect(
        DATABASE_URL, row_factory=dict_row, options=f"-c TimeZone={APP_TIMEZONE}")


def split_sql(sql: str) -> list[str]:
    """ตัด schema.sql เป็นคำสั่งย่อย โดยไม่ตัดกลาง string / comment / บล็อก $$...$$

    ต้องรู้จัก dollar-quote เพราะ body ของ touch_updated_at() มี ";" อยู่ข้างใน
    ถ้า split ด้วย ";" เฉย ๆ function จะขาดกลางแล้ว syntax error
    """
    stmts, buf = [], []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):           # comment ท้ายบรรทัด
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "'":                                        # string ธรรมดา
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if sql.startswith("''", j):
                        j += 2
                        continue
                    break
                j += 1
            buf.append(sql[i : j + 1])
            i = j + 1
            continue
        if ch == "$":                                        # dollar-quote: $$ หรือ $tag$
            end_tag = sql.find("$", i + 1)
            inner = sql[i + 1 : end_tag] if end_tag != -1 else None
            if inner is not None and (inner == "" or inner.replace("_", "").isalnum()):
                tag = sql[i : end_tag + 1]
                close = sql.find(tag, end_tag + 1)
                if close != -1:
                    buf.append(sql[i : close + len(tag)])
                    i = close + len(tag)
                    continue
        if ch == ";":
            if (stmt := "".join(buf).strip()):
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if (stmt := "".join(buf).strip()):
        stmts.append(stmt)
    return stmts


def init_schema(*, verbose: bool = False) -> None:
    """สร้าง/อัปเดต schema ทีละคำสั่ง commit ทีละคำสั่ง

    เดิมยิงไฟล์ทั้งก้อนใน transaction เดียว ซึ่งพังทั้งหมดถ้า connection หลุดกลางทาง
    (Postgres ฝั่ง Render ตัดสายเป็นระยะ) แล้ว rollback ทุกอย่างที่ทำไปแล้วด้วย
    ทุกคำสั่งในไฟล์เขียนแบบรันซ้ำได้ (IF NOT EXISTS / CREATE OR REPLACE) จึงต่อสายใหม่
    แล้วรันคำสั่งเดิมซ้ำได้อย่างปลอดภัย
    """
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    if DB_SCHEMA != "ocr_dhammakaya":
        sql = sql.replace("ocr_dhammakaya", DB_SCHEMA)
    stmts = split_sql(sql)
    conn = connect()
    try:
        for idx, stmt in enumerate(stmts, 1):
            for attempt in (1, 2, 3):
                try:
                    conn.execute(stmt)
                    conn.commit()
                    break
                except psycopg.OperationalError as exc:
                    if attempt == 3:
                        raise
                    print(f"[{idx}/{len(stmts)}] connection หลุด ({exc}) — ต่อใหม่แล้วลองอีกครั้ง")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = connect()
            if verbose:
                print(f"[{idx}/{len(stmts)}] {' '.join(stmt.split())[:80]}")
    finally:
        conn.close()


# ---------- เขียนข้อมูล ----------

def build_row(fields: dict[str, Any]) -> dict[str, Any]:
    """แปลงค่าที่คนยืนยันแล้ว เป็นคอลัมน์ในตาราง พร้อมคำนวณคอลัมน์ *_norm สำหรับค้นหา"""
    plate = fields.get("noplate")
    province = fields.get("province")
    date = parse_date(fields.get("date"))
    return {
        "name": fields.get("name"),
        "name_norm": norm_name(fields.get("name")),
        "tel": fields.get("tel"),
        "tel_digits": norm_phone(fields.get("tel")),
        "plate_raw": plate,
        "plate_norm": norm_plate(plate),
        "province": norm_province(province) or None,
        "brand": fields.get("brand"),
        "brand_norm": norm_brand(fields.get("brand")),
        "car_type": norm_cartype(fields.get("typecar")) or None,
        "location": fields.get("location"),
        "deposit_date": date,
    }


def insert_slip(
    conn: psycopg.Connection,
    fields: dict[str, Any],
    *,
    confidence: dict[str, float],
    raw_ocr: dict[str, Any],
    review_reason: list[str],
    ocr_model: str,
    ocr_variant: str,
    usage: dict[str, Any] | None = None,
    latency_s: float = 0.0,
    created_by: str | None = None,
    uploaded_by: str | None = None,
    photographer: str | None = None,
    review_status: str = "pending",
    entry_source: str | None = None,
) -> str:
    row = build_row(fields)
    row.update(
        review_status=review_status,
        needs_review=bool(review_reason),
        review_reason=review_reason,
        ocr_model=ocr_model,
        ocr_variant=ocr_variant,
        ocr_confidence=json.dumps(confidence, ensure_ascii=False),
        ocr_cost_usd=(usage or {}).get("cost") or 0,
        ocr_tokens_in=(usage or {}).get("prompt_tokens") or 0,
        ocr_tokens_out=(usage or {}).get("completion_tokens") or 0,
        ocr_latency_s=round(latency_s, 2),
        raw_ocr=json.dumps(raw_ocr, ensure_ascii=False),
        created_by=created_by,
        uploaded_by=uploaded_by,
        entry_source=entry_source,
        # ถ้าไม่ได้ระบุคนถ่าย ให้ถือว่าเป็นคนเดียวกับคนอัปโหลด
        photographer=photographer or uploaded_by,
    )
    cols = ", ".join(row)
    holders = ", ".join(f"%({c})s" for c in row)
    cur = conn.execute(
        f"INSERT INTO {DB_SCHEMA}.slips ({cols}) VALUES ({holders}) RETURNING id", row
    )
    return str(cur.fetchone()["id"])


def add_image(
    conn: psycopg.Connection, slip_id: str, kind: str, jpeg: bytes, size: tuple[int, int]
) -> None:
    """เก็บรูปหลักฐานของใบนี้ — กันซ้ำเฉพาะภายในใบเดียวกันเท่านั้น

    ห้ามกันซ้ำข้ามใบ ไม่งั้นการอัปโหลดรูปเดิมซ้ำจะได้เรคอร์ดที่ไม่มีรูปหลักฐานติดอยู่
    """
    conn.execute(
        f"""INSERT INTO {DB_SCHEMA}.slip_images (slip_id, kind, sha256, width, height, bytes)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (slip_id, sha256) DO NOTHING""",
        (slip_id, kind, hashlib.sha256(jpeg).hexdigest(), size[0], size[1], jpeg),
    )


def replace_image(
    conn: psycopg.Connection, slip_id: str, kind: str, jpeg: bytes, size: tuple[int, int]
) -> None:
    """เปลี่ยนรูปของใบนี้เป็นไฟล์ใหม่ (ใช้ตอน reprocess ภาพที่ crop ผิด)

    ลบของเดิมก่อนเพื่อไม่ให้เหลือรูปเก่าค้าง เพราะ get_image หยิบรูปที่เก่าที่สุดของ kind นั้น
    """
    conn.execute(
        f"DELETE FROM {DB_SCHEMA}.slip_images WHERE slip_id = %s AND kind = %s", (slip_id, kind)
    )
    add_image(conn, slip_id, kind, jpeg, size)


def slip_with_image(conn: psycopg.Connection, jpeg: bytes) -> dict[str, Any] | None:
    """ใบที่ถือรูปนี้ (byte ตรงกันเป๊ะ) อยู่แล้ว — None = ยังไม่เคยเห็นรูปนี้

    คืน "ใบ" ไม่ใช่ True/False เพราะคนที่อัปซ้ำต้องได้รู้ว่าใบเดิมคือใบไหนและอยู่สถานะไหน
    เอาใบเก่าสุดเสมอ ถ้าเคยมีซ้ำอยู่แล้วก็ให้ทุกคนชี้ไปที่ใบเดียวกัน ไม่ใช่ไล่ชี้ต่อกันเป็นลูกโซ่
    """
    cur = conn.execute(
        f"""SELECT s.id::text AS id, s.name, s.tel, s.plate_raw, s.review_status,
                   s.uploaded_by, s.created_at
            FROM {DB_SCHEMA}.slip_images i JOIN {DB_SCHEMA}.slips s ON s.id = i.slip_id
            WHERE i.sha256 = %s ORDER BY s.created_at LIMIT 1""",
        (hashlib.sha256(jpeg).hexdigest(),),
    )
    return cur.fetchone()


def update_slip(
    conn: psycopg.Connection,
    slip_id: str,
    fields: dict[str, Any],
    *,
    edited_by: str | None,
    review_status: str | None = None,
    review_reason: list[str] | None = None,
    require_status: str | None = None,
) -> int | None:
    """บันทึกค่าที่คนแก้ + เขียน audit log เฉพาะ field ที่เปลี่ยนจริง คืนจำนวน field ที่แก้

    require_status = สถานะที่ใบต้องเป็นอยู่ ณ ตอนเขียน (optimistic lock)
    คืน None ถ้าสถานะไม่ตรง แปลว่ามีคนอื่นตรวจใบนี้ไปก่อนแล้วระหว่างที่หน้านี้เปิดค้างอยู่
    ต้องไม่เขียนอะไรเลยในกรณีนั้น ไม่งั้นงานของคนแรกถูกทับเงียบ ๆ พร้อม audit log ซ้ำอีกชุด
    """
    before = get_slip(conn, slip_id)
    row = build_row(fields)
    if review_status is not None:
        row["review_status"] = review_status
        row["needs_review"] = bool(review_reason)
        row["review_reason"] = review_reason or []
        row["reviewed_by"] = edited_by
        row["reviewed_at"] = "now()"
        # ใบที่ออกจากกอง pending แล้วไม่ต้องมีใครถืออีก ปล่อยคืนพร้อมกันในคำสั่งเดียว
        row["claimed_by"] = row["claimed_name"] = row["claimed_at"] = None

    sets = ", ".join(f"{c} = %({c})s" for c in row if c != "reviewed_at")
    if review_status is not None:
        sets += ", reviewed_at = now()"
    row.pop("reviewed_at", None)
    where = "WHERE id = %(id)s"
    if require_status is not None:
        where += " AND review_status = %(require_status)s"
    cur = conn.execute(
        f"UPDATE {DB_SCHEMA}.slips SET {sets} {where}",
        {**row, "id": slip_id, "require_status": require_status},
    )
    if cur.rowcount == 0:
        return None

    changed = 0
    for col in ("name", "tel", "plate_raw", "province", "brand", "car_type", "location", "deposit_date"):
        old, new = before.get(col), row.get(col)
        if str(old or "") != str(new or ""):
            conn.execute(
                f"""INSERT INTO {DB_SCHEMA}.slip_edits (slip_id, field, old_value, new_value, edited_by)
                    VALUES (%s, %s, %s, %s, %s)""",
                (slip_id, col, str(old) if old is not None else None,
                 str(new) if new is not None else None, edited_by),
            )
            changed += 1
    return changed


def mark_returned(
    conn: psycopg.Connection,
    slip_id: str,
    by: str | None,
    note: str | None,
    released_to: str | None = None,
) -> bool:
    """ปิดใบว่ารับรถกลับแล้ว คืน False ถ้าใบนี้ถูกปิดไปก่อนแล้ว (ไม่เขียนทับของคนที่กดก่อน)

    เงื่อนไข car_status = 'stored' ต้องอยู่ "ใน" UPDATE ไม่ใช่เช็คก่อนแล้วค่อยเขียน —
    ที่จุด checkout มีเจ้าหน้าที่หลายคนหันจอคนละเครื่อง การกดใบเดียวกันพร้อมกันเกิดขึ้นจริง
    ถ้าปล่อยให้ทับได้ ชื่อคนส่งมอบกับเวลาจะกลายเป็นของคนที่กดทีหลัง ซึ่งคือการลบร่องรอย
    ของคนที่ปล่อยรถไปจริง (ปัญหาเดียวกับที่คอมมิต 66fdce4 แก้ไว้ที่ขั้นอนุมัติ)
    """
    cur = conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips
            SET car_status = 'returned', returned_at = now(), returned_by = %s,
                returned_note = %s, released_to = %s
            WHERE id = %s AND car_status = 'stored'""",
        (by, note, released_to, slip_id),
    )
    return cur.rowcount == 1


def norm_loc(alias: str) -> str:
    """นิพจน์ SQL เทียบที่จอดแบบไม่ถือสาช่องว่าง/ตัวพิมพ์ ("อาคาร 1  ชั้น 2" = "อาคาร 1 ชั้น 2")

    OCR อ่านใบกระดาษใบเดียวกันสองรูปได้ช่องว่างไม่เท่ากันเป็นเรื่องปกติ ถ้าเทียบตรง ๆ
    ใบซ้ำจริงจะหลุดการจับเพราะเว้นวรรคต่างกันอย่างเดียว
    """
    return f"lower(btrim(regexp_replace(coalesce({alias}.location, ''), '\\s+', ' ', 'g')))"


# ใบซ้ำสองแบบที่ต้องแยกกัน:
#   * รูปต้นฉบับ hash ตรงกัน = ไฟล์เดียวกันถูกยิงเข้ามาสองรอบ ชัดเจน 100% ถอนออกจากคิวได้เลย
#   * ทะเบียน+เบอร์+วันที่+ที่จอด ตรงกัน = ใบกระดาษใบเดียวกันถูกถ่ายสองรูป (คนละมุม hash จึงต่าง)
#
# แบบหลังเดาจากค่าในใบ จึงต้องกัน "การฝากรอบใหม่" ให้หลุดออกไปสองชั้น:
#   1. วันที่ฝากต้องตรงกัน — กันรถคันเดิมที่เอามาฝากใหม่เดือนหน้า
#   2. ที่จอดต้องตรงกัน — กันรอบใหม่ "ในวันเดียวกัน" (เช้าฝาก บ่ายรับ เย็นฝากอีก)
#      ซึ่งข้อ 1 กันไม่ได้เลย เพราะทะเบียน/เบอร์/วันที่ตรงกันหมดทั้งที่เป็นคนละรอบ
#      รอบใหม่ได้ช่องจอดใหม่เสมอ ส่วนใบกระดาษใบเดียวกันสองรูปย่อมเขียนที่จอดเดียวกัน
#   3. ต้องยัง stored ทั้งคู่ — ใบที่คืนรถไปแล้วปิดรอบของตัวเองไปแล้ว ใบถัดมาคือรอบใหม่
#      (ชั้นนี้ช่วยเฉพาะตอนงานเอกสารตามหลังของจริง ไม่ใช่ด่านหลัก)
#
# พลาดทางไหนก็ได้ไม่เท่ากัน: ถ้าเดาว่า "ไม่ซ้ำ" ผิด คนตรวจเสียเวลาทำใบซ้ำใบเดียว
# ถ้าเดาว่า "ซ้ำ" ผิด รถจอดอยู่จริงแต่ไม่มีใบ active ไปโผล่เอาตอนเจ้าของมารับแล้วหาใบไม่เจอ
# เงื่อนไขชุดนี้จึงเอียงไปทางปล่อยให้ค้างคิวไว้ก่อน
def same_slip() -> str:
    """เงื่อนไข SQL ว่าใบ d กับใบ k เป็นใบเดียวกัน

    ต้องเป็นฟังก์ชัน ไม่ใช่ค่าคงที่ระดับโมดูล — f-string ที่ประกอบตอน import จะฝังชื่อ
    schema ณ ตอนนั้นไว้ตายตัว พอเทสต์ชี้ schema อื่น คำสั่งยังวิ่งไป ocr_dhammakaya
    เหมือนเดิม (บั๊กเดียวกับที่ reject_slip เคยเจอ)
    """
    return f"""(
    EXISTS (SELECT 1 FROM {DB_SCHEMA}.slip_images a
                     JOIN {DB_SCHEMA}.slip_images b ON a.sha256 = b.sha256
             WHERE a.slip_id = d.id AND b.slip_id = k.id)
    OR (coalesce(k.plate_norm, '') <> '' AND coalesce(k.tel_digits, '') <> ''
        AND k.deposit_date IS NOT NULL AND coalesce(btrim(k.location), '') <> ''
        AND d.plate_norm = k.plate_norm AND d.tel_digits = k.tel_digits
        AND d.deposit_date = k.deposit_date
        AND {norm_loc('d')} = {norm_loc('k')}
        AND d.car_status = 'stored' AND k.car_status = 'stored')
)"""


def reject_slip(
    conn: psycopg.Connection, slip_id: str, reason: str, reviewer: str | None
) -> None:
    """ตีกลับใบที่ใช้ไม่ได้ (รูปเบลอ / ไม่ใช่ใบฝากรถ) — ไม่แตะค่าข้อมูลในใบ

    SQL ต้องอยู่ที่นี่ ไม่ใช่ใน route: เดิมเขียนชื่อ schema ตายตัวไว้ใน main.py
    คำสั่งจึงไปลง ocr_dhammakaya เสมอ ไม่ว่า DB_SCHEMA จะถูกตั้งเป็นอะไร —
    ตอนรันเทสต์ที่ชี้ schema อื่น การตีกลับจึงเงียบหาย (0 แถว) แต่ยังตอบ 303 เหมือนสำเร็จ
    """
    conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips
               SET review_status = 'rejected', needs_review = false, review_reason = %s,
                   reviewed_by = %s, reviewed_at = now(),
                   -- ใบที่ออกจากกอง pending แล้วไม่ต้องมีใครถืออีก
                   claimed_by = NULL, claimed_name = NULL, claimed_at = NULL
             WHERE id = %s""",
        ([reason], reviewer, slip_id),
    )


def mark_superseded(conn: psycopg.Connection, keeper_id: str) -> int:
    """ถอนใบที่ยังค้างคิวและเป็นใบเดียวกับ keeper ออกจากคิว คืนจำนวนใบที่ถอน

    เรียกทันทีหลังอนุมัติ — จุดนั้นคือจุดเดียวที่รู้แน่ว่า "ใบนี้มีคนตรวจแล้ว"
    ที่เหลือที่เหมือนกันจึงเป็นของซ้ำที่ไม่ต้องให้ใครตรวจอีก

    แตะเฉพาะใบที่ยัง pending เท่านั้น ใบที่ตรวจไปแล้ว (ทั้งอนุมัติและตีกลับ) ไม่แตะ —
    งานที่คนทำไปแล้วต้องไม่ถูกกลบด้วยการเดาของสคริปต์ ถ้ามีใบซ้ำที่อนุมัติไปแล้ว
    ให้ admin ตัดสินใจลบเองจากรายงาน (python -m ocrslip.dedup)
    """
    cur = conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips d
               SET superseded_by = k.id, superseded_at = now(),
                   -- ใบที่ออกจากคิวแล้วไม่ต้องมีใครถืออีก
                   claimed_by = NULL, claimed_name = NULL, claimed_at = NULL
              FROM {DB_SCHEMA}.slips k
             WHERE k.id = %(keep)s AND d.id <> k.id
               AND d.review_status = 'pending' AND d.superseded_by IS NULL
               AND {same_slip()}""",
        {"keep": keeper_id},
    )
    return cur.rowcount


def delete_slip(conn: psycopg.Connection, slip_id: str) -> dict[str, Any] | None:
    """ลบใบถาวร คืนข้อมูลใบที่ลบไป (None ถ้าไม่มีใบนี้แล้ว)

    รูปหลักฐานกับประวัติการแก้ไขหายตามไปด้วยผ่าน ON DELETE CASCADE ซึ่งคือสิ่งที่ต้องการ —
    ของที่ลบคือใบขยะ (ใบทดสอบ กรอกมั่ว ยิงซ้ำ) การเก็บซากไว้มีแต่ทำให้ตัวเลขสรุปเพี้ยน

    คืนแถวที่ลบด้วย DELETE ... RETURNING ไม่ใช่ SELECT ก่อนแล้วค่อย DELETE
    เพื่อให้สิ่งที่บันทึกลง log เป็นแถวที่ถูกลบไปจริง ๆ ไม่ใช่แถวที่อ่านมาตอนนั้น
    """
    cur = conn.execute(
        f"""DELETE FROM {DB_SCHEMA}.slips WHERE id = %s
            RETURNING id::text AS id, name, tel, plate_raw, car_status, entry_source""",
        (slip_id,),
    )
    return cur.fetchone()

# ---------- ค่าตั้งที่แก้จากหน้าเว็บได้ ----------

def get_settings(conn: psycopg.Connection) -> dict[str, str]:
    """ค่าตั้งทั้งหมดที่เคยถูกบันทึกจากหน้าเว็บ (คีย์ที่ไม่เคยตั้งจะไม่อยู่ใน dict)"""
    cur = conn.execute(f"SELECT key, value FROM {DB_SCHEMA}.app_settings")
    return {r["key"]: r["value"] for r in cur.fetchall()}


def set_setting(conn: psycopg.Connection, key: str, value: str, by: str | None = None) -> None:
    """บันทึกค่าตั้ง — ค่าว่างแปลว่า "เลิกตั้งจากหน้าเว็บ" จึงลบแถวทิ้งให้ตกไปใช้ env

    ต้องลบ ไม่ใช่เก็บสตริงว่างไว้ ไม่งั้นการล้างช่องในหน้าตั้งค่าจะกลายเป็นการ
    ตั้งค่าเป็น "ว่าง" ทับค่าใน env แทนที่จะเป็นการถอยกลับไปใช้ค่าตั้งต้น
    """
    if not value.strip():
        conn.execute(f"DELETE FROM {DB_SCHEMA}.app_settings WHERE key = %s", (key,))
        return
    conn.execute(
        f"""INSERT INTO {DB_SCHEMA}.app_settings (key, value, updated_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()""",
        (key, value.strip(), by),
    )

# ---------- อ่านข้อมูล ----------

def open_slip_by_plate(
    conn: psycopg.Connection, plate_norm: str, *, hours: int = 12
) -> dict[str, Any] | None:
    """ใบของทะเบียนนี้ที่ยังไม่ได้รับรถกลับและเพิ่งลงทะเบียนไปไม่นาน

    ใช้กันการกดส่งฟอร์มขาเข้าซ้ำ (refresh หน้า / กดปุ่มสองที) ไม่ให้รถคันเดียวได้สองใบ
    ใบซ้ำไม่ได้เจ็บตอนบันทึก แต่เจ็บตอนขาออก — เจ้าหน้าที่เห็นสองแถวเหมือนกัน
    แล้วไม่รู้ว่าต้องปิดใบไหน ปิดผิดใบก็เหลือใบค้างที่ไม่มีใครมารับตลอดไป
    """
    cur = conn.execute(
        f"""SELECT * FROM {DB_SCHEMA}.slips
            WHERE plate_norm = %s AND car_status = 'stored'
              AND created_at > now() - (%s || ' hours')::interval
            ORDER BY created_at DESC LIMIT 1""",
        (plate_norm, hours),
    )
    return cur.fetchone()

def deposit_rounds(
    conn: psycopg.Connection, plate_norms: list[str]
) -> dict[str, dict[str, Any]]:
    """ใบของทะเบียนที่ถูกเอามาฝากหลายรอบ -> {slip_id: {"round": n, "total": m}}

    คนกลุ่มหนึ่งเอารถมาฝาก รับกลับ แล้วเอามาฝากใหม่ เห็นมาแล้ว 2-3 รอบต่อคัน
    ข้อมูลถูกอยู่แล้ว (1 ใบ = 1 รอบ) แต่หน้าค้นหาแสดงเป็นแถวคล้าย ๆ กันเรียงตามคะแนน
    เจ้าหน้าที่ขาออกจึงต้องไล่อ่านวันที่เองว่าใบไหนคือรอบปัจจุบัน — ปิดผิดใบเมื่อไหร่
    จะเหลือใบค้างที่ไม่มีใครมารับตลอดไป การบอกเลขรอบตรง ๆ ถูกกว่าให้คนเดาเอง

    คืนเฉพาะทะเบียนที่มีมากกว่า 1 รอบ — ใบเดี่ยว ๆ ติดป้าย "รอบที่ 1 จาก 1" มีแต่รกตา
    ไม่นับใบที่ถูกตีว่าซ้ำหรือตีกลับ เพราะไม่ใช่การฝากจริงสักรอบ
    """
    plates = [p for p in {p for p in plate_norms} if p]
    if not plates:
        return {}
    rows = conn.execute(
        f"""SELECT id::text AS id, round, total FROM (
                SELECT id,
                       row_number() OVER w AS round,
                       count(*) OVER (PARTITION BY plate_norm) AS total
                  FROM {DB_SCHEMA}.slips
                 WHERE plate_norm = ANY(%s)
                   AND superseded_by IS NULL AND review_status <> 'rejected'
                WINDOW w AS (PARTITION BY plate_norm
                             ORDER BY deposit_date NULLS LAST, created_at)
            ) t WHERE total > 1""",
        (plates,),
    ).fetchall()
    return {r["id"]: {"round": r["round"], "total": r["total"]} for r in rows}


def deposit_history(conn: psycopg.Connection, plate_norm: str) -> list[dict[str, Any]]:
    """ทุกรอบการฝากของทะเบียนนี้ เรียงตามรอบ — ไว้โชว์ในหน้าใบตอนจะปล่อยรถ

    เจ้าหน้าที่ต้องเห็นได้ทันทีว่าใบที่เปิดอยู่คือรอบไหน และรอบอื่นปิดไปหมดหรือยัง
    """
    if not plate_norm:
        return []
    return conn.execute(
        f"""SELECT id::text AS id, round, deposit_date, location, car_status,
                   review_status, returned_at
              FROM (SELECT id, deposit_date, location, car_status, review_status,
                           returned_at,
                           row_number() OVER (ORDER BY deposit_date NULLS LAST,
                                              created_at) AS round
                      FROM {DB_SCHEMA}.slips
                     WHERE plate_norm = %s
                       AND superseded_by IS NULL AND review_status <> 'rejected') t
             ORDER BY round""",
        (plate_norm,),
    ).fetchall()


def get_slip(conn: psycopg.Connection, slip_id: str) -> dict[str, Any]:
    cur = conn.execute(f"SELECT * FROM {DB_SCHEMA}.slips WHERE id = %s", (slip_id,))
    return cur.fetchone() or {}



def list_edits(conn: psycopg.Connection, slip_id: str) -> list[dict[str, Any]]:
    """ประวัติการแก้ค่าของใบนี้ ใหม่สุดขึ้นก่อน"""
    cur = conn.execute(
        f"SELECT * FROM {DB_SCHEMA}.slip_edits WHERE slip_id = %s ORDER BY edited_at DESC",
        (slip_id,),
    )
    return cur.fetchall()

def get_image(conn: psycopg.Connection, slip_id: str, kind: str = "processed") -> bytes | None:
    cur = conn.execute(
        f"""SELECT bytes FROM {DB_SCHEMA}.slip_images
            WHERE slip_id = %s AND kind = %s ORDER BY created_at LIMIT 1""",
        (slip_id, kind),
    )
    row = cur.fetchone()
    return bytes(row["bytes"]) if row else None


def list_slips(
    conn: psycopg.Connection,
    *,
    review_status: str | None = None,
    needs_review: bool | None = None,
    car_status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where, params = [], []
    if review_status:
        where.append("review_status = %s")
        params.append(review_status)
    if needs_review is not None:
        where.append("needs_review = %s")
        params.append(needs_review)
    if car_status:
        where.append("car_status = %s")
        params.append(car_status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    cur = conn.execute(
        f"SELECT * FROM {DB_SCHEMA}.slips {clause} ORDER BY created_at DESC LIMIT %s",
        (*params, limit),
    )
    return cur.fetchall()


def next_in_queue(conn: psycopg.Connection, slip_id: str) -> tuple[str | None, int]:
    """(id ใบถัดไปในคิว, จำนวนใบที่ยังค้าง) — ถามฐานข้อมูลตรง ๆ

    เดิมดึงคิวทั้งกอง (SELECT * 200 แถว พร้อม jsonb ของ OCR) มาเรียงใน Python
    เพื่อเอาแค่ใบแรกกับจำนวน ซึ่งหนักเกินเหตุเพราะหน้านี้เปิดทุกครั้งที่ตรวจ 1 ใบ

    เรียงให้ใบที่ต้องตรวจมาก่อน แล้วไล่จากใบเก่าสุด (เคลียร์งานตกค้างให้หมดก่อน)
    """
    # สอง subquery ใน statement เดียว = คุยรอบเดียว แต่ไม่ต้องใช้ count(*) OVER ()
    # ซึ่งบังคับให้อ่านแถวที่ sort แล้วทั้งกอง (วัดที่ 66,000 ใบ: 96 ms -> 25 ms)
    row = conn.execute(
        f"""SELECT (SELECT id FROM {DB_SCHEMA}.slips
                     WHERE review_status = 'pending' AND superseded_by IS NULL
                       AND id <> %(id)s
                     ORDER BY needs_review DESC, created_at ASC LIMIT 1) AS next_id,
                   (SELECT count(*) FROM {DB_SCHEMA}.slips
                     WHERE review_status = 'pending' AND superseded_by IS NULL
                       AND id <> %(id)s) AS remaining""",
        {"id": slip_id},
    ).fetchone()
    return (str(row["next_id"]) if row["next_id"] else None), row["remaining"]


# ---------- การจองใบในคิว ----------
#
# ปัญหาที่แก้: next_in_queue() ยื่นใบหัวแถวใบเดียวกันให้ทุกคน สามคนที่นั่งตรวจพร้อมกัน
# จึงได้ใบเดียวกันเสมอ แล้วเสียเวลาทำซ้ำของกันและกัน
#
# การจองเป็นแค่คำแนะนำ ไม่ใช่การล็อก — คนถือใบแล้วปิดแท็บเกิดขึ้นตลอด ถ้าล็อกแข็ง
# จะมีใบที่แตะไม่ได้ค้างเต็มคิว ตัวที่การันตีว่าข้อมูลไม่ทับกันยังเป็น require_status
# ใน update_slip() เหมือนเดิม การจองแค่ทำให้ "ไม่ค่อยเจอกัน" ส่วน write guard
# ทำให้ "เจอกันแล้วไม่พัง"

CLAIM_COLS = "claimed_by = NULL, claimed_name = NULL, claimed_at = NULL"


def release_claims(conn: psycopg.Connection, worker: str) -> None:
    """ปล่อยใบที่คนนี้ถืออยู่ทั้งหมด — คนหนึ่งถือได้ทีละใบเสมอ

    เรียกก่อนจองใบใหม่ทุกครั้ง ไม่งั้นคนที่กดข้ามไปเรื่อย ๆ จะทิ้งใบที่จองค้างไว้
    เต็มคิว แล้วคนอื่นต้องรอจนหมดอายุทั้งที่ไม่มีใครตรวจอยู่จริง
    """
    conn.execute(
        f"UPDATE {DB_SCHEMA}.slips SET {CLAIM_COLS}"
        f" WHERE claimed_by = %s AND review_status = 'pending'",
        (worker,),
    )


def claim_next(conn: psycopg.Connection, worker: str, name: str | None = None) -> str | None:
    """ปล่อยใบเดิมแล้วจองใบถัดไปในคิว คืน id ที่จองได้ (None = คิวหมด)

    ต้องมีทั้งสองเงื่อนไขในคำสั่งเดียว เพราะกันคนละกรณีกัน:

    * claimed_at — กันใบที่คนอื่นจองไปแล้วและ commit แล้ว (การชนระดับนาที)
    * FOR UPDATE SKIP LOCKED — กันสอง transaction ที่ยิงพร้อมกันแล้วยังไม่ commit
      แย่งแถวเดียวกัน (การชนระดับมิลลิวินาที) ตัวนี้ไม่รู้จักใบที่จอง+commit ไปแล้ว
      ส่วน claimed_at ก็ไม่เห็น transaction ที่ยังค้างอยู่ ขาดตัวใดตัวหนึ่งไม่ได้

    เรียงเหมือน next_in_queue เดิม: ใบที่ต้องตรวจมาก่อน แล้วไล่จากใบเก่าสุด
    """
    release_claims(conn, worker)
    row = conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips
               SET claimed_by = %(me)s, claimed_name = %(name)s, claimed_at = now()
             WHERE id = (SELECT id FROM {DB_SCHEMA}.slips
                          WHERE review_status = 'pending'
                            AND superseded_by IS NULL
                            AND (claimed_at IS NULL
                                 OR claimed_at < now() - %(lease)s * interval '1 minute')
                          ORDER BY needs_review DESC, created_at ASC
                          LIMIT 1 FOR UPDATE SKIP LOCKED)
         RETURNING id::text AS id""",
        {"me": worker, "name": name, "lease": REVIEW_CLAIM_MINUTES},
    ).fetchone()
    return row["id"] if row else None


def claim_one(
    conn: psycopg.Connection, slip_id: str, worker: str, name: str | None = None
) -> dict[str, Any] | None:
    """จองใบที่ระบุ (คนคลิกจากรายการคิว) คืน None ถ้าจองได้

    ถ้าจองไม่ได้ คืนแถวของคนที่ถืออยู่ไว้เอาไปบอกบนหน้าจอ — เตือนเฉย ๆ ไม่บล็อก
    เพราะอาจเป็นคนเดียวกันเปิดจากอีกเครื่อง หรือเขาตั้งใจเข้ามาดูใบนี้จริง ๆ
    """
    row = conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips
               SET claimed_by = %(me)s, claimed_name = %(name)s, claimed_at = now()
             WHERE id = %(id)s AND review_status = 'pending'
               AND (claimed_at IS NULL
                    OR claimed_at < now() - %(lease)s * interval '1 minute'
                    OR claimed_by = %(me)s)
         RETURNING id""",
        {"id": slip_id, "me": worker, "name": name, "lease": REVIEW_CLAIM_MINUTES},
    ).fetchone()
    if row:
        release_other_claims(conn, worker, keep=slip_id)
        return None
    held = conn.execute(
        f"""SELECT claimed_name, claimed_at FROM {DB_SCHEMA}.slips
             WHERE id = %(id)s AND review_status = 'pending'
               AND claimed_at >= now() - %(lease)s * interval '1 minute'""",
        {"id": slip_id, "lease": REVIEW_CLAIM_MINUTES},
    ).fetchone()
    return held


def release_other_claims(conn: psycopg.Connection, worker: str, keep: str) -> None:
    """ปล่อยใบอื่นที่คนนี้ถืออยู่ เหลือไว้ใบเดียวคือใบที่กำลังเปิด"""
    conn.execute(
        f"UPDATE {DB_SCHEMA}.slips SET {CLAIM_COLS}"
        f" WHERE claimed_by = %s AND id <> %s AND review_status = 'pending'",
        (worker, keep),
    )


def known_people(conn: psycopg.Connection) -> list[str]:
    """รายชื่อที่ยังใช้งานอยู่ ไว้ให้เลือกตอนอัปโหลด"""
    cur = conn.execute(
        f"SELECT name FROM {DB_SCHEMA}.staff_members WHERE active ORDER BY name"
    )
    return [r["name"] for r in cur.fetchall()]


def list_staff(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """รายชื่อทั้งหมด + จำนวนใบที่แต่ละคน "อัปโหลด" และ "ตรวจ" (ไว้ให้ admin ดูว่าใครทำไปเท่าไร)

    คนอัปกับคนตรวจเป็นคนละบทบาท แถวเดียวกันจึงต้องนับแยกสองช่อง
    ใช้ LEFT JOIN กับยอดที่ group มาแล้วรอบเดียว ไม่ใช้ subquery ต่อแถว
    ไม่งั้นมีกี่ชื่อก็ต้องกวาดตาราง slips เท่านั้นรอบ
    เทียบชื่อแบบ lower(btrim()) ให้ตรงกับที่ build_filters ใช้ ตัวเลขบนหน้านี้
    จะได้เท่ากับจำนวนแถวที่กดเข้าไปดูจริง
    """
    cur = conn.execute(
        f"""SELECT m.*,
                   coalesce(up.n, 0)        AS slips,
                   coalesce(rv.approved, 0) AS approved,
                   coalesce(rv.rejected, 0) AS rejected
            FROM {DB_SCHEMA}.staff_members m
            LEFT JOIN (
                SELECT lower(btrim(uploaded_by)) AS k, count(*) AS n
                FROM {DB_SCHEMA}.slips WHERE uploaded_by IS NOT NULL GROUP BY 1
            ) up ON up.k = lower(btrim(m.name))
            LEFT JOIN (
                SELECT lower(btrim(reviewed_by)) AS k,
                       count(*) FILTER (WHERE review_status = 'approved') AS approved,
                       count(*) FILTER (WHERE review_status = 'rejected') AS rejected
                FROM {DB_SCHEMA}.slips WHERE reviewed_by IS NOT NULL GROUP BY 1
            ) rv ON rv.k = lower(btrim(m.name))
            ORDER BY m.active DESC, m.name"""
    )
    return cur.fetchall()


# ค่าที่ฟอร์มใช้สื่อว่า "ขอพิมพ์ชื่อใหม่" ห้ามหลุดเข้าไปเป็นชื่อคนจริง
# ไม่งั้นจะโผล่เป็นตัวเลือกซ้ำในรายการ และคนที่เลือกมันจะถูกตีความเป็น sentinel ตลอดไป
NEW_NAME_SENTINEL = "__new__"

# อักขระที่มองไม่เห็น (zero-width, BOM) .strip() เอาออกไม่ได้
# ถ้าปล่อยผ่านจะได้ชื่อที่ว่างเปล่าในสายตาคน แต่ระบบนับว่ามีค่า
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff\u00ad]")


def clean_person_name(name: str | None) -> str:
    """ชื่อคนที่ใช้ได้จริง — ตัดอักขระล่องหนออก และปฏิเสธค่า sentinel"""
    cleaned = _INVISIBLE.sub("", str(name or "")).strip()
    if cleaned == NEW_NAME_SENTINEL:
        return ""
    return cleaned


def add_staff(conn: psycopg.Connection, name: str, created_by: str | None = None) -> bool:
    """เพิ่มชื่อเข้ารายการ คืน False ถ้าชื่อซ้ำ (เทียบแบบไม่สนตัวพิมพ์และช่องว่างหัวท้าย)"""
    name = clean_person_name(name)
    if not name:
        return False
    cur = conn.execute(
        f"""INSERT INTO {DB_SCHEMA}.staff_members (name, created_by) VALUES (%s, %s)
            ON CONFLICT DO NOTHING RETURNING id""",
        (name, created_by),
    )
    return cur.fetchone() is not None


def set_staff_active(conn: psycopg.Connection, staff_id: int, active: bool) -> None:
    """ปิดการใช้งานแทนการลบ เพื่อให้ใบเก่าที่อ้างชื่อนี้ยังตามรอยได้"""
    conn.execute(
        f"UPDATE {DB_SCHEMA}.staff_members SET active = %s WHERE id = %s", (active, staff_id)
    )


def review_counts(conn: psycopg.Connection) -> dict[str, int]:
    cur = conn.execute(
        f"""SELECT
              -- กองที่ต้องทำต้องไม่นับใบซ้ำ ไม่งั้นยอดบนแถบกองจะไม่ตรงกับจำนวนแถวที่เห็น
              count(*) FILTER (WHERE review_status = 'pending' AND superseded_by IS NULL
                               AND needs_review)                                       AS needs_review,
              count(*) FILTER (WHERE review_status = 'pending' AND superseded_by IS NULL
                               AND NOT needs_review)                                   AS quick_pass,
              count(*) FILTER (WHERE superseded_by IS NOT NULL)                        AS superseded,
              count(*) FILTER (WHERE review_status = 'approved')                       AS approved,
              count(*) FILTER (WHERE review_status = 'rejected')                       AS rejected,
              count(*) FILTER (WHERE car_status = 'stored' AND review_status='approved') AS stored,
              count(*)                                                                 AS total
            FROM {DB_SCHEMA}.slips"""
    )
    return dict(cur.fetchone())


def export_rows(conn: psycopg.Connection, filters: tuple[str, dict] | None = None) -> Iterable[dict]:
    """แถวสำหรับ Excel — ใช้ filter ชุดเดียวกับหน้าตาราง"""
    where, params = filters or ("TRUE", {})
    cur = conn.execute(
        f"""SELECT name, tel, plate_raw, province, brand, car_type, location, deposit_date,
                   review_status, car_status, returned_at, returned_by,
                   uploaded_by, photographer, reviewed_by, ocr_model, created_at
            FROM {DB_SCHEMA}.slips WHERE {where} ORDER BY created_at""",
        params,
    )
    return cur.fetchall()

# ---------- ตารางข้อมูล + dashboard ----------

def build_filters(
    q: str | None = None,
    review_status: str | None = None,
    needs_review: bool | None = None,
    car_status: str | None = None,
    car_type: str | None = None,
    superseded: bool | None = None,
    uploaded_by: str | None = None,
    reviewed_by: str | None = None,
) -> tuple[str, dict]:
    """สร้าง WHERE clause ที่ใช้ร่วมกันระหว่างหน้าตาราง, คิวตรวจ, ตัวนับ และ Excel

    ใช้ตัวเดียวกันทุกที่ เพื่อให้ปุ่ม 'โหลด Excel' ได้ข้อมูลตรงกับที่เห็นบนจอเสมอ

    ค้นบนคอลัมน์ *_norm ไม่ใช่คอลัมน์ดิบ เพราะ trigram index อยู่บน *_norm
    ถ้า ILIKE คอลัมน์ดิบ index ใช้ไม่ได้เลย ทุกการค้นจะกวาดทั้งตาราง
    """
    where, params = ["TRUE"], {}
    if review_status:
        where.append("review_status = %(review_status)s")
        params["review_status"] = review_status
    if needs_review is not None:
        where.append("needs_review = %(needs_review)s")
        params["needs_review"] = needs_review
    if car_status:
        where.append("car_status = %(car_status)s")
        params["car_status"] = car_status
    if car_type:
        where.append("car_type = %(car_type)s")
        params["car_type"] = car_type
    # ใบซ้ำไม่ได้หายไปจากระบบ แค่ไม่อยู่ในกองที่ต้องทำ — จึงเป็นตัวกรอง ไม่ใช่การซ่อนถาวร
    if superseded is not None:
        where.append(f"superseded_by IS {'NOT NULL' if superseded else 'NULL'}")
    # กรองตามคน: เทียบแบบไม่สนตัวพิมพ์/ช่องว่างหัวท้าย เพราะชื่อที่พิมพ์เองตอนอัปโหลดหรือตอนตรวจ
    # อาจต่างจากชื่อในรายการแค่ช่องว่าง แล้วจะกลายเป็นคนละคนในสายตา query
    for key, val in (("uploaded_by", uploaded_by), ("reviewed_by", reviewed_by)):
        name = clean_person_name(val)
        if name:
            where.append(f"lower(btrim({key})) = lower(btrim(%({key})s))")
            params[key] = name
    if q and q.strip():
        # normalize คำค้นแบบเดียวกับตอนบันทึก ไม่งั้นพิมพ์ "นายสมชาย" จะไม่เจอแถวที่เก็บ "สมชาย"
        # ใส่เฉพาะช่องที่ normalize แล้วยังเหลือข้อความ ไม่งั้น LIKE '%%' จะแมตช์ทุกแถว
        parts = []
        for col, key, val in (
            ("name_norm", "qname", norm_name(q)),
            ("plate_norm", "qplate", norm_plate(q)),
            ("brand_norm", "qbrand", norm_brand(q)),
            ("location", "qloc", _base(q).strip()),   # ที่จอดไม่มีคอลัมน์ norm ใช้ trgm บนคอลัมน์ดิบ
        ):
            if val:
                parts.append(f"{col} ILIKE %({key})s")
                params[key] = f"%{val}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        if digits:  # ถ้าไม่มีตัวเลขเลย ห้ามใส่เงื่อนไขเบอร์ ไม่งั้น LIKE '%%' จะแมตช์ทุกแถว
            parts.append("tel_digits LIKE %(qd)s")
            params["qd"] = f"%{digits}%"
        if parts:
            where.append(f"({' OR '.join(parts)})")
    return " AND ".join(where), params


SORTABLE = {
    "created_at": "created_at", "name": "name_norm", "tel": "tel_digits",
    "uploaded_by": "uploaded_by", "reviewed_by": "reviewed_by",
    "plate": "plate_norm", "brand": "brand_norm", "date": "deposit_date",
    "car_status": "car_status", "review_status": "review_status",
}


def query_slips(
    conn: psycopg.Connection, *, filters: tuple[str, dict],
    sort: str = "created_at", desc: bool = True, page: int = 1, per_page: int = 50,
    total: int | None = None,
) -> tuple[list[dict], int]:
    """(แถวของหน้านี้, จำนวนทั้งหมดที่เข้าเงื่อนไข) ในการคุยกับฐานข้อมูลรอบเดียว

    ส่ง total มาได้ถ้าผู้เรียกรู้ยอดอยู่แล้ว (หน้าคิวตรวจรู้จาก review_counts)
    จะได้ไม่ต้องนับใหม่เลย

    ฐานข้อมูลอยู่คนละเครื่องกับเว็บ RTT วัดได้ 60-550 ms ขณะที่งานฝั่ง Postgres
    ใช้ไม่ถึง 1 ms ฉะนั้นตัวที่กินเวลาคือ "จำนวนรอบที่คุย" ไม่ใช่ความหนักของ query
    count(*) OVER () จึงคุ้มกว่าการยิง COUNT(*) แยกอีกรอบ
    แลกกับการที่ window ต้องอ่านแถวที่แมตช์ทั้งหมด วัดบน dataset สังเคราะห์แล้ว:

        จำนวนใบ    count(*) OVER ()    COUNT(*) แยกรอบ (+1 RTT ~67 ms)
        1,700              1.3 ms                      ~68 ms
        33,000            96.5 ms                      ~92 ms
        200,000          408.0 ms                      ~75 ms

    จุดคุ้มทุนอยู่ราว 20,000-30,000 ใบ ต่ำกว่านั้น window ชนะ สูงกว่านั้นให้แยก COUNT
    (ตอนนี้ของจริงมี ~1,700 ใบ) ส่วนหน้าคิวตรวจไม่ต้องใช้ทางไหนเลยถ้าไม่ได้ค้นหา
    เพราะ review_counts() ให้ยอดของทุกกองมาอยู่แล้วในรอบที่ยิงไปแล้ว

    ไม่ SELECT * เพราะจะลาก raw_ocr / ocr_confidence (jsonb ก้อนใหญ่) มาเปล่า ๆ
    ทั้งที่หน้าตารางกับคิวตรวจไม่ได้ใช้ — วัดแล้วต่างกันหลายเท่าตัวบนสายจริง
    """
    where, params = filters
    order = SORTABLE.get(sort, "created_at")
    counter = "" if total is not None else ", count(*) OVER () AS total_rows"
    rows = conn.execute(
        f"""SELECT id, name, tel, plate_raw, province, brand, car_type, location,
                   deposit_date, review_status, needs_review, review_reason,
                   car_status, returned_at, returned_by, uploaded_by, photographer,
                   reviewed_by, ocr_model, created_at,
                   claimed_name, claimed_at, superseded_by,
                   -- คำนวณที่ฐานข้อมูลเพราะเวลาของ Postgres คือตัวเดียวกับที่ใช้ตัดสิน
                   -- ว่าการจองหมดอายุหรือยัง ถ้าไปเทียบฝั่ง Python นาฬิกาคนละตัวกัน
                   (claimed_at IS NOT NULL
                    AND claimed_at >= now() - %(lease)s * interval '1 minute') AS claim_live{counter}
            FROM {DB_SCHEMA}.slips WHERE {where}
            ORDER BY {order} {'DESC' if desc else 'ASC'} NULLS LAST
            LIMIT %(limit)s OFFSET %(offset)s""",
        {**params, "limit": per_page, "offset": (page - 1) * per_page,
         "lease": REVIEW_CLAIM_MINUTES},
    ).fetchall()
    if total is None:
        if rows:
            total = rows[0]["total_rows"]
            for r in rows:
                del r["total_rows"]
        elif page > 1:
            # หน้าว่างเพราะเลยหน้าสุดท้ายไป ต้องถามจำนวนจริงเพื่อให้ปุ่มแบ่งหน้ายังถูก
            total = conn.execute(
                f"SELECT count(*) AS n FROM {DB_SCHEMA}.slips WHERE {where}", params
            ).fetchone()["n"]
        else:
            total = 0
    return rows, total


def dashboard_stats(conn: psycopg.Connection) -> dict[str, Any]:
    """สรุปตัวเลขทั้งหมดในการ query ไม่กี่ครั้ง — หน้า dashboard ต้องเบา"""
    kpi = dict(conn.execute(
        f"""SELECT count(*) AS total,
                   count(*) FILTER (WHERE review_status='approved')            AS approved,
                   count(*) FILTER (WHERE review_status='pending')             AS pending,
                   count(*) FILTER (WHERE review_status='pending' AND needs_review) AS needs_review,
                   count(*) FILTER (WHERE review_status='rejected')            AS rejected,
                   count(*) FILTER (WHERE car_status='stored')                 AS stored,
                   count(*) FILTER (WHERE car_status='returned')               AS returned,
                   coalesce(sum(ocr_cost_usd), 0)                              AS cost_usd,
                   coalesce(avg(ocr_cost_usd) FILTER (WHERE ocr_cost_usd > 0), 0) AS avg_cost_usd,
                   coalesce(avg(ocr_latency_s) FILTER (WHERE ocr_latency_s > 0), 0) AS avg_latency
            FROM {DB_SCHEMA}.slips"""
    ).fetchone())

    def group(col: str, limit: int = 8) -> list[dict]:
        return conn.execute(
            f"""SELECT coalesce(nullif({col}, ''), '— ไม่ระบุ —') AS label, count(*) AS n
                FROM {DB_SCHEMA}.slips WHERE review_status <> 'rejected'
                GROUP BY 1 ORDER BY n DESC LIMIT %s""",
            (limit,),
        ).fetchall()

    # แกนวันต้องเป็น "ทุกวันใน 30 วันล่าสุด" ไม่ใช่ "14 วันที่บังเอิญมีใบ"
    # ของเดิมเอาวันที่มีใบมาเรียงติดกัน วันว่างจึงหายไปจากแกน และวันที่ OCR อ่านผิดปี
    # (2083, 2027) ถูกวาดเป็นแท่งข้าง ๆ 2026 โดยป้ายโชว์แค่ วว/ดด — อ่านแล้วเข้าใจผิดว่าเรียงกัน
    by_day = conn.execute(
        f"""WITH days AS (
                SELECT generate_series(current_date - 29, current_date, '1 day')::date AS day
            )
            SELECT d.day AS label,
                   count(s.id) AS n,
                   count(s.id) FILTER (WHERE s.car_status='returned') AS returned
            FROM days d
            LEFT JOIN {DB_SCHEMA}.slips s
                   ON s.deposit_date = d.day AND s.review_status <> 'rejected'
            GROUP BY 1 ORDER BY 1"""
    ).fetchall()

    # ใบที่ไม่ได้อยู่ในกราฟข้างบน ต้องบอกจำนวนไว้เสมอ ไม่งั้นกราฟจะดูเหมือนข้อมูลทั้งหมด
    # ทั้งที่จริงมีใบตกขอบอยู่หลักร้อย (วันที่ว่าง / ปีที่เป็นไปไม่ได้ / วันที่เก่ากว่า 30 วัน)
    date_health = dict(conn.execute(
        f"""SELECT count(*) FILTER (WHERE deposit_date IS NULL) AS no_date,
                   count(*) FILTER (WHERE deposit_date < current_date - 365
                                       OR deposit_date > current_date + 1) AS odd_date,
                   count(*) FILTER (WHERE deposit_date BETWEEN current_date - 365
                                                           AND current_date - 30) AS older,
                   count(*) FILTER (WHERE deposit_date BETWEEN current_date - 29
                                                           AND current_date + 1) AS in_window
            FROM {DB_SCHEMA}.slips WHERE review_status <> 'rejected'"""
    ).fetchone())

    # คุณภาพ OCR: ช่องไหนที่คนต้องแก้บ่อยที่สุด (มาจาก audit log ของการใช้งานจริง)
    edits = conn.execute(
        f"""SELECT field AS label, count(*) AS n FROM {DB_SCHEMA}.slip_edits
            GROUP BY 1 ORDER BY n DESC LIMIT 8"""
    ).fetchall()
    # ใครอนุมัติ/ตีกลับไปกี่ใบ — คนละชุดกับ by_uploader เพราะคนอัปกับคนตรวจไม่ใช่คนเดียวกัน
    # รวมกลุ่ม "ไม่ระบุ" ไว้ด้วย (ใบเก่าที่ตรวจก่อนจะมีช่องชื่อผู้ตรวจ) ไม่งั้นยอดรวมจะไม่ตรงกับ approved
    #
    # จัดกลุ่มด้วย lower(btrim()) แบบเดียวกับ build_filters แล้วใช้ mode() เลือกตัวสะกดที่พบบ่อยสุด
    # เป็นชื่อที่แสดง ไม่งั้น "first" กับ "FIRST" จะเป็นสองแถวบนจอ แต่กดเข้าไปได้ใบชุดเดียวกัน
    # (= ตัวเลขบนแถวไม่ตรงกับจำนวนที่เห็นจริง)
    by_reviewer = conn.execute(
        f"""SELECT coalesce(mode() WITHIN GROUP (ORDER BY btrim(reviewed_by)), '— ไม่ระบุ —') AS label,
                   count(*) FILTER (WHERE review_status = 'approved') AS n,
                   count(*) FILTER (WHERE review_status = 'rejected') AS rejected
            FROM {DB_SCHEMA}.slips
            WHERE reviewed_at IS NOT NULL
            GROUP BY lower(btrim(reviewed_by))
            ORDER BY n DESC, rejected DESC LIMIT 20"""
    ).fetchall()

    reviewed = conn.execute(
        f"SELECT count(*) AS n FROM {DB_SCHEMA}.slips WHERE reviewed_at IS NOT NULL"
    ).fetchone()["n"]

    reasons = conn.execute(
        f"""SELECT unnest(review_reason) AS label, count(*) AS n
            FROM {DB_SCHEMA}.slips WHERE cardinality(review_reason) > 0
            GROUP BY 1 ORDER BY n DESC"""
    ).fetchall()

    return {
        "kpi": kpi,
        "by_type": group("car_type"),
        "by_brand": group("brand_norm"),
        "by_location": group("location"),
        "by_uploader": conn.execute(
            f"""SELECT coalesce(mode() WITHIN GROUP (ORDER BY btrim(uploaded_by)), '— ไม่ระบุ —') AS label,
                       count(*) AS n
                FROM {DB_SCHEMA}.slips WHERE review_status <> 'rejected'
                GROUP BY lower(btrim(uploaded_by)) ORDER BY n DESC LIMIT 8"""
        ).fetchall(),
        "by_reviewer": by_reviewer,
        "by_day": by_day,
        "date_health": date_health,
        "edits": edits,
        "reviewed": reviewed,
        "reasons": reasons,
    }



if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "init":
        init_schema()
        with connect() as c:
            cur = c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename",
                (DB_SCHEMA,),
            )
            print(f"schema {DB_SCHEMA} พร้อมใช้งาน:", [r["tablename"] for r in cur.fetchall()])
    else:
        print(__doc__)
