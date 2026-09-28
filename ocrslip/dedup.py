"""รายงานใบซ้ำ + ถอนใบซ้ำที่ยังค้างคิวออกจากคิวตรวจ

ใบซ้ำเกิดจากการอัปรูปชุดเดิมเข้ามาสองรอบ (ช่องเลือกไฟล์ไม่ถูกล้างหลังอัปสำเร็จ
คนที่คิดว่าเมื่อกี้ไม่ติดจึงกดอีกที) ผลคือคนตรวจได้ใบที่เพื่อนตรวจไปแล้วมาทำซ้ำ
เหมือนงานที่ทำไปไม่ได้บันทึก ต้นเหตุถูกปิดที่ ingest แล้ว สคริปต์นี้ไว้เก็บของที่ค้างอยู่

จับซ้ำสองแบบ — แบบเดียวกับ db.same_slip() ที่ใช้ตอนอนุมัติ:
  * รูปต้นฉบับ hash ตรงกัน = ไฟล์เดียวกันถูกยิงเข้ามาสองรอบ
  * ทะเบียน + เบอร์ + วันที่ฝาก + ที่จอด ตรงกัน = ใบกระดาษใบเดียวกันถูกถ่ายสองรูป
    (hash จึงต่าง) — ต้องยังไม่คืนรถทั้งคู่ด้วย ดูเหตุผลของแต่ละเงื่อนไขที่ db.same_slip()

--apply ทำแค่อย่างเดียว: ใบที่ยัง pending และมีพี่น้องที่ "อนุมัติไปแล้ว" จะถูกตีว่า
เป็นของซ้ำ (superseded_by) แล้วหลุดออกจากคิว — ไม่ลบ ไม่แตะค่าข้อมูล ไม่แตะใบที่ตรวจแล้ว
ใบซ้ำที่อนุมัติไปแล้วทั้งคู่ สคริปต์นี้จะรายงานไว้ให้เท่านั้น เพราะการเลือกว่าจะทิ้งใบไหน
เป็นเรื่องที่คนต้องดูของจริง (ลบได้จากหน้าเว็บที่ /search -> เปิดใบ -> ลบใบนี้)

    python -m ocrslip.dedup            # ดูรายงาน ไม่เขียนอะไร
    python -m ocrslip.dedup --apply    # ถอนใบซ้ำที่ค้างคิวออกจากคิวจริง
    python -m ocrslip.dedup --limit 20 # จำกัดจำนวนกลุ่มที่พิมพ์รายละเอียด
"""

from __future__ import annotations

import argparse
import re
from typing import Any

import psycopg

from .config import DB_SCHEMA
from .db import connect, mark_superseded

STATUS_LABEL = {"pending": "รอตรวจ", "approved": "อนุมัติแล้ว", "rejected": "ตีกลับ"}


def norm_location(value: str | None) -> str:
    """ที่จอดแบบไม่ถือสาช่องว่าง/ตัวพิมพ์ — ฝั่ง Python ของ db.norm_loc() ต้องให้ผลเหมือนกัน"""
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


SLIP_COLS = """s.id::text AS id, s.name, s.tel, s.plate_raw, s.plate_norm, s.tel_digits,
                   s.province, s.brand, s.car_type, s.deposit_date, s.location,
                   s.review_status, s.reviewed_by, s.superseded_by, s.uploaded_by,
                   s.car_status, s.returned_at, s.created_at"""

# ตัวกรอง "ใบที่มีสิทธิ์ซ้ำกับใบอื่น" — คีย์หลวมกว่า group_duplicates โดยเจตนา
# (ไม่ดูที่จอด/สถานะรถ) เพราะมันเป็นแค่ด่านตัดของที่ไม่เกี่ยวออกก่อนส่งให้ union-find
# ตัวตัดสินจริงยังเป็น group_duplicates ตัวเดียว คีย์ที่หลวมกว่าครอบคีย์จริงอยู่แล้ว
# จึงไม่มีใบไหนในกลุ่มจริงหลุดหายไป (ถ้าทำให้แคบกว่า กลุ่มจะขาดสมาชิกแบบเงียบ ๆ)
# ต้องเป็น JOIN ไม่ใช่ "id IN (... OR ...)" — แบบหลังทำให้ planner ไล่ EXISTS กับ
# row-comparison ทีละแถวของทั้งตาราง วัดได้ 11 วินาที ขณะที่ JOIN กับ UNION ใช้ไม่ถึงวินาที
CANDIDATES = """
    WITH img AS (SELECT slip_id, sha256 FROM {s}.slip_images WHERE kind = 'original'),
         dup_sha AS (SELECT sha256 FROM img GROUP BY sha256
                      HAVING count(DISTINCT slip_id) > 1),
         dup_txt AS (SELECT plate_norm, tel_digits, deposit_date FROM {s}.slips
                      WHERE coalesce(plate_norm, '') <> '' AND coalesce(tel_digits, '') <> ''
                        AND deposit_date IS NOT NULL
                      GROUP BY 1, 2, 3 HAVING count(*) > 1)
    SELECT i.slip_id AS id FROM img i JOIN dup_sha USING (sha256)
     UNION
    SELECT s2.id FROM {s}.slips s2 JOIN dup_txt t
       ON s2.plate_norm = t.plate_norm AND s2.tel_digits = t.tel_digits
      AND s2.deposit_date = t.deposit_date
"""


def load_slips(
    conn: psycopg.Connection, *, only_candidates: bool = False
) -> list[dict[str, Any]]:
    """ทุกใบ + hash ของรูปต้นฉบับ (ใบที่กรอกเองไม่มีรูป จึงเป็น NULL)

    only_candidates = เอาเฉพาะใบที่มีสิทธิ์ซ้ำ ใช้ตอนหน้าเว็บเรียก — ทั้งตาราง
    (5,700 แถว) ใช้เวลา 1.7 วินาทีบนสายจริง ซึ่งช้าเกินไปสำหรับหน้าที่กดวนหลายร้อยครั้ง
    สคริปต์ CLI ยังโหลดทั้งตารางตามเดิม เพราะรันทีเดียวจบและอยากให้เห็นภาพรวมจริง
    """
    osha = f"""(SELECT i.sha256 FROM {DB_SCHEMA}.slip_images i
                     WHERE i.slip_id = s.id AND i.kind = 'original' LIMIT 1) AS osha"""
    if not only_candidates:
        sql = f"SELECT {SLIP_COLS}, {osha} FROM {DB_SCHEMA}.slips s ORDER BY s.created_at"
    else:
        sql = (f"WITH cand AS ({CANDIDATES.format(s=DB_SCHEMA)})"
               f" SELECT {SLIP_COLS}, {osha} FROM {DB_SCHEMA}.slips s"
               f" JOIN cand ON cand.id = s.id ORDER BY s.created_at")
    return conn.execute(sql).fetchall()


def group_duplicates(slips: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """จัดใบที่เป็น "ใบเดียวกัน" ไว้กลุ่มเดียวกัน คืนเฉพาะกลุ่มที่มีมากกว่า 1 ใบ

    ใช้ union-find เพราะใบสามใบอาจเกาะกันคนละทาง (A กับ B รูปเดียวกัน, B กับ C
    ทะเบียนเดียวกัน) ถ้าจัดกลุ่มแยกตามคีย์ทีละแบบ กลุ่มเดียวกันจะถูกนับสองรอบ
    """
    parent: dict[str, str] = {s["id"]: s["id"] for s in slips}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    first: dict[tuple, str] = {}
    for s in slips:
        keys = []
        if s["osha"]:
            keys.append(("img", s["osha"]))
        # คีย์ต้องตรงกับ db.same_slip() เป๊ะ ไม่งั้นรายงานจะบอกคนละเรื่องกับสิ่งที่ --apply ทำ
        # (วันที่ + ที่จอด + ยังไม่คืนรถ = สามชั้นที่กันการฝากรอบใหม่ไม่ให้ถูกตีว่าซ้ำ)
        loc = norm_location(s["location"])
        if (s["plate_norm"] and s["tel_digits"] and s["deposit_date"] and loc
                and s["car_status"] == "stored"):
            keys.append(("txt", s["plate_norm"], s["tel_digits"], s["deposit_date"], loc))
        for key in keys:
            if key in first:
                union(first[key], s["id"])
            else:
                first[key] = s["id"]

    groups: dict[str, list[dict[str, Any]]] = {}
    for s in slips:
        groups.setdefault(find(s["id"]), []).append(s)
    return [g for g in groups.values() if len(g) > 1]


def keeper_of(group: list[dict[str, Any]]) -> dict[str, Any] | None:
    """ใบที่ถือเป็นตัวจริงของกลุ่ม = ใบที่อนุมัติแล้วและเก่าสุด (None = ยังไม่มีใครตรวจ)

    ต้องเป็นใบที่อนุมัติแล้วเท่านั้น กลุ่มที่ยังไม่มีใครตรวจต้องปล่อยให้คนตรวจใบใดใบหนึ่ง
    ตามปกติ — ตอนกดอนุมัติ ระบบจะถอนที่เหลือออกจากคิวให้เอง
    """
    approved = [s for s in group if s["review_status"] == "approved"]
    return min(approved, key=lambda s: s["created_at"]) if approved else None


def describe(group: list[dict[str, Any]], keep: dict[str, Any] | None) -> str:
    lines = []
    for s in sorted(group, key=lambda s: s["created_at"]):
        mark = "  *" if keep and s["id"] == keep["id"] else "   "
        tag = STATUS_LABEL.get(s["review_status"], s["review_status"])
        if s["superseded_by"]:
            tag += " / ถูกตีว่าซ้ำแล้ว"
        lines.append(
            f"{mark} {s['id'][:8]} {s['created_at'].strftime('%d/%m %H:%M')} "
            f"[{tag}] {s['plate_raw'] or '—'} {s['name'] or '—'} "
            f"อัปโดย {s['uploaded_by'] or '—'}"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="รายงาน/เก็บกวาดใบซ้ำ")
    ap.add_argument("--apply", action="store_true",
                    help="ถอนใบซ้ำที่ยังค้างคิวออกจากคิวจริง (ไม่ใส่ = ดูอย่างเดียว)")
    ap.add_argument("--limit", type=int, default=15, help="จำนวนกลุ่มที่พิมพ์รายละเอียด")
    args = ap.parse_args()

    with connect() as conn:
        groups = group_duplicates(load_slips(conn))
        groups.sort(key=lambda g: min(s["created_at"] for s in g))

        extra = sum(len(g) - 1 for g in groups)
        print(f"กลุ่มใบซ้ำ {len(groups)} กลุ่ม — ใบเกินรวม {extra} ใบ "
              f"(มากสุด {max((len(g) for g in groups), default=0)} ใบต่อกลุ่ม)")

        # สามกองที่ต้องจัดการต่างกัน
        clearable = [g for g in groups
                     if keeper_of(g) and any(s["review_status"] == "pending"
                                             and not s["superseded_by"] for s in g)]
        untouched = [g for g in groups if not keeper_of(g)]
        approved_dups = [g for g in groups
                         if len([s for s in g if s["review_status"] == "approved"]) > 1]

        pending_extra = sum(len([s for s in g if s["review_status"] == "pending"
                                 and not s["superseded_by"]]) for g in clearable)
        print(f"  · ถอนออกจากคิวได้เลย: {pending_extra} ใบ ใน {len(clearable)} กลุ่ม "
              f"(มีใบที่อนุมัติไปแล้วเป็นตัวจริงอยู่)")
        print(f"  · ยังไม่มีใครตรวจทั้งกลุ่ม: {len(untouched)} กลุ่ม "
              f"— ปล่อยให้คนตรวจใบใดใบหนึ่ง ที่เหลือจะถอนออกเองตอนกดอนุมัติ")
        print(f"  · ซ้ำกันทั้งที่อนุมัติไปแล้ว: {len(approved_dups)} กลุ่ม "
              f"— ต้องคนตัดสินว่าจะลบใบไหน (เปิดใบจากหน้าค้นหาแล้วกดลบ)")

        if clearable:
            print(f"\nตัวอย่างกลุ่มที่ถอนออกจากคิวได้ ({min(args.limit, len(clearable))} "
                  f"จาก {len(clearable)} กลุ่ม, * = ใบตัวจริง):")
            for g in clearable[: args.limit]:
                print(describe(g, keeper_of(g)))
                print()

        if approved_dups:
            print(f"กลุ่มที่อนุมัติซ้ำไปแล้ว ({min(args.limit, len(approved_dups))} "
                  f"จาก {len(approved_dups)} กลุ่ม) — สคริปต์นี้ไม่แตะ:")
            for g in approved_dups[: args.limit]:
                print(describe(g, keeper_of(g)))
                print()

        if not args.apply:
            print("(ดูอย่างเดียว — ใส่ --apply เพื่อถอนใบซ้ำที่ค้างคิวออกจากคิวจริง)")
            return

        moved = 0
        for g in clearable:
            # ต้องไล่ใบที่อนุมัติแล้ว "ทุกใบ" ในกลุ่ม ไม่ใช่แค่ตัวจริง — กลุ่มหนึ่งถูกเกาะไว้ด้วยกัน
            # ด้วยคีย์คนละแบบ ใบที่ค้างคิวจึงอาจเป็นใบเดียวกับใบที่อนุมัติใบอื่นในกลุ่ม
            # ไม่ใช่ใบที่เก่าสุด (mark_superseded ข้ามใบที่ถูกตีว่าซ้ำไปแล้ว จึงไม่นับซ้ำ)
            for k in sorted((s for s in g if s["review_status"] == "approved"),
                            key=lambda s: s["created_at"]):
                moved += mark_superseded(conn, k["id"])
        conn.commit()
        print(f"ถอนใบซ้ำออกจากคิวแล้ว {moved} ใบ — ข้อมูลกับรูปหลักฐานยังอยู่ครบ "
              f"ดูได้ที่คิวตรวจ กอง \"ซ้ำ\"")


if __name__ == "__main__":
    main()
