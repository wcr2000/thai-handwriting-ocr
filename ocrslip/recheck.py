"""ดึงใบที่ "ยืนยันไว้ตอนที่ระบบยัง crop รูปพลาด" กลับเข้าคิวตรวจ

ช่วงที่ตัวจับขอบกระดาษยังพัง คนตรวจเห็นรูปที่บิด/หมุน/ขอบขาด จึงอาจกดยืนยันข้อมูล
ที่ผิดไปโดยไม่รู้ตัว ตัวอย่างที่เจอจริง: ใบหนึ่งเก็บทะเบียนกับยี่ห้อเป็นของรถคนละคัน
ทั้งที่ในรูปเขียนไว้ชัดเจน

เกณฑ์: ให้ model อ่าน "ภาพคนละมุม" ของใบเดียวกัน (ภาพที่ crop แล้ว กับภาพเต็ม)
ถ้าทั้งสองมุมได้ค่าตรงกันเอง แต่ต่างจากค่าที่เก็บไว้มาก ถึงจะหยิบมาให้คนดูซ้ำ

ต้องเป็น "คนละภาพ" เท่านั้น — การอ่านภาพเดิมซ้ำสองรอบไม่ใช่หลักฐานอะไรเลย
เพราะ temperature=0 ภาพเดิมย่อมได้คำตอบเดิม

ข้อจำกัดที่ต้องรู้: การที่ model อ่านได้ค่าเดิมจากสองมุม ไม่ได้แปลว่าค่าที่คนกรอก
ผิดเสมอไป — ลายมือที่กำกวมจริง ๆ model ก็อ่านผิดเหมือนกันได้ทั้งสองมุม
สคริปต์นี้จึง "ไม่เขียนทับข้อมูล" เด็ดขาด แค่เปลี่ยนสถานะกลับเป็น pending
พร้อมเหตุผล recheck_bad_crop ให้คนตรวจตัดสินเองจากรูปที่ถูกต้องแล้ว

    python -m ocrslip.recheck            # ดูว่าจะดึงใบไหนกลับ
    python -m ocrslip.recheck --apply
"""

from __future__ import annotations

import argparse
from typing import Any

import psycopg
from rapidfuzz.distance import Levenshtein

from .config import DB_SCHEMA, OCR_MODEL
from .db import connect, get_image
from .imageio import encode_jpeg
from .normalize import normalize_field
from .ocr import read_slip
from .preprocess import preprocess

# ช่องที่ใช้ตัดสิน — เป็นช่องที่ใช้ตามหารถจริง ๆ ถ้าผิดคือหารถไม่เจอ
CHECKED = ("name", "tel", "noplate")
AGREE_MAX = 0.15        # ต่างกันเองได้ไม่เกินเท่านี้ ถึงจะนับว่า "สองมุมอ่านตรงกัน"
CONFLICT_MIN = 0.5      # ต่างจากค่าที่เก็บไว้เกินเท่านี้ ถึงจะนับว่าขัดแย้งจริง


def _cer(truth: str, got: str) -> float:
    if not truth:
        return 0.0 if not got else 1.0
    return min(1.0, Levenshtein.distance(truth, got) / len(truth))


def conflicts(stored: dict[str, Any], reads: list[dict[str, Any]]) -> list[str]:
    """ช่องที่อ่านจากทุกมุมได้ค่าตรงกัน แต่ขัดกับค่าที่เก็บไว้

    reads ต้องมาจากภาพคนละแบบของใบเดียวกัน ไม่ใช่ภาพเดิมอ่านซ้ำ
    """
    out = []
    for field in CHECKED:
        values = [normalize_field(field, r.get(field)) for r in reads]
        kept = normalize_field(field, stored.get(field))
        if not kept or not values[0]:
            continue
        if all(_cer(values[0], v) <= AGREE_MAX for v in values[1:]) \
                and _cer(kept, values[0]) > CONFLICT_MIN:
            out.append(field)
    return out


def scan(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT id::text AS id, name, tel, plate_raw FROM {DB_SCHEMA}.slips
            WHERE review_status = 'approved'
              AND NOT ('recheck_bad_crop' = ANY(review_reason))
            ORDER BY created_at"""
    ).fetchall()

    found = []
    for i, row in enumerate(rows, 1):
        original = get_image(conn, row["id"], "original")
        if original is None:
            continue
        pre = preprocess(original)
        # สองมุมที่เป็นอิสระจากกันจริง: ภาพที่ crop แล้ว กับภาพเต็มทั้งเฟรม
        views = [encode_jpeg(pre.cropped), encode_jpeg(pre.raw)]
        reads = []
        for jpeg in views:
            res = read_slip(jpeg, OCR_MODEL)
            if not res.ok:
                break
            reads.append(res.fields)
        if len(reads) < len(views):
            continue

        stored = {"name": row["name"], "tel": row["tel"], "noplate": row["plate_raw"]}
        bad = conflicts(stored, reads)
        print(f"[{i}/{len(rows)}] {row['id'][:8]} " + (f"ขัดแย้ง: {bad}" if bad else "ok"))
        if bad:
            found.append({"id": row["id"], "fields": bad, "stored": stored, "read": reads[0]})
    return found


def send_back(conn: psycopg.Connection, slip_id: str) -> None:
    """คืนสถานะเป็น pending พร้อมเหตุผล — ไม่แตะค่าข้อมูลเลยสักช่อง"""
    conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips
            SET review_status = 'pending', needs_review = true,
                review_reason = (SELECT array_agg(DISTINCT r)
                                 FROM unnest(array_append(review_reason,
                                                          'recheck_bad_crop')) r),
                reviewed_by = NULL, reviewed_at = NULL,
                -- ใบที่ดึงกลับเข้าคิวต้องไม่พกการจองเก่ามาด้วย ไม่งั้นมันถูกถือโดยคน
                -- ที่ไม่ได้นั่งอยู่แล้ว และจะไม่ถูกจ่ายให้ใครจนกว่าการจองจะหมดอายุ
                claimed_by = NULL, claimed_name = NULL, claimed_at = NULL
            WHERE id = %s""",
        (slip_id,),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="ดึงใบที่ยืนยันไว้ตอน crop พังกลับเข้าคิวตรวจ")
    ap.add_argument("--apply", action="store_true", help="เขียนจริง (ไม่ใส่ = ดูอย่างเดียว)")
    args = ap.parse_args()

    with connect() as conn:
        found = scan(conn)
        print(f"\nใบที่ควรตรวจซ้ำ: {len(found)} ใบ "
              f"(model อ่านได้ค่าเดิมจากทั้งสองมุม แต่ต่างจากที่เก็บไว้ "
              f"— ไม่ได้แปลว่าค่าที่เก็บไว้ผิดเสมอไป คนตรวจตัดสินอีกที)")
        for f in found:
            for field in f["fields"]:
                print(f"  {f['id'][:8]} [{field}] ในระบบ={f['stored'][field]!r} "
                      f"อ่านใหม่ได้={f['read'].get(field)!r}")
        if not args.apply:
            print("\n(ดูอย่างเดียว — ใส่ --apply เพื่อดึงกลับเข้าคิวจริง)")
            return
        for f in found:
            send_back(conn, f["id"])
        conn.commit()
        print(f"\nดึงกลับเข้าคิวแล้ว {len(found)} ใบ — ข้อมูลเดิมไม่ถูกแตะ คนตรวจจะเห็นรูปที่ถูกต้องแล้ว")


if __name__ == "__main__":
    main()
