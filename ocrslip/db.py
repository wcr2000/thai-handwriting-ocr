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

from .config import DATABASE_URL, DB_SCHEMA
from .normalize import (
    _base, norm_brand, norm_cartype, norm_name, norm_phone, norm_plate, norm_province, parse_date,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = ROOT / "db" / "schema.sql"


def connect() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("ยังไม่ได้ตั้ง DATABASE_URL ใน .env")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


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


def image_seen(conn: psycopg.Connection, jpeg: bytes) -> bool:
    """เคยอัปโหลดรูปนี้ (byte ตรงกันเป๊ะ) มาก่อนหรือยัง"""
    cur = conn.execute(
        f"SELECT 1 FROM {DB_SCHEMA}.slip_images WHERE sha256 = %s LIMIT 1",
        (hashlib.sha256(jpeg).hexdigest(),),
    )
    return cur.fetchone() is not None


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


def mark_returned(conn: psycopg.Connection, slip_id: str, by: str | None, note: str | None) -> None:
    conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips
            SET car_status = 'returned', returned_at = now(), returned_by = %s, returned_note = %s
            WHERE id = %s""",
        (by, note, slip_id),
    )


# ---------- อ่านข้อมูล ----------

def get_slip(conn: psycopg.Connection, slip_id: str) -> dict[str, Any]:
    cur = conn.execute(f"SELECT * FROM {DB_SCHEMA}.slips WHERE id = %s", (slip_id,))
    return cur.fetchone() or {}


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
                     WHERE review_status = 'pending' AND id <> %(id)s
                     ORDER BY needs_review DESC, created_at ASC LIMIT 1) AS next_id,
                   (SELECT count(*) FROM {DB_SCHEMA}.slips
                     WHERE review_status = 'pending' AND id <> %(id)s) AS remaining""",
        {"id": slip_id},
    ).fetchone()
    return (str(row["next_id"]) if row["next_id"] else None), row["remaining"]


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
              count(*) FILTER (WHERE review_status = 'pending' AND needs_review)       AS needs_review,
              count(*) FILTER (WHERE review_status = 'pending' AND NOT needs_review)   AS quick_pass,
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
                   reviewed_by, ocr_model, created_at{counter}
            FROM {DB_SCHEMA}.slips WHERE {where}
            ORDER BY {order} {'DESC' if desc else 'ASC'} NULLS LAST
            LIMIT %(limit)s OFFSET %(offset)s""",
        {**params, "limit": per_page, "offset": (page - 1) * per_page},
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
