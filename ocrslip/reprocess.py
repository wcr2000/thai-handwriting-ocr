"""crop รูปที่เก็บไว้ใหม่ แล้ว OCR ซ้ำเฉพาะใบที่ยังไม่มีคนยืนยัน

ใช้หลังแก้ตัวจับขอบกระดาษ: ใบเก่าถูก crop ไปโดนพื้นหลัง/กระดาษเปล่า หรือโดนตัดขอบ
ภาพที่ส่งเข้า model จึงเอียง/หมุน/ขอบขาด และอ่านผิดโดยไม่มีสัญญาณเตือน

    python -m ocrslip.reprocess            # ดูว่าจะกระทบใบไหนบ้าง (ไม่เขียนอะไร)
    python -m ocrslip.reprocess --apply    # ลงมือแก้จริง

กฎความปลอดภัย: รูป original ไม่ถูกแตะต้องเลย และข้อมูลของใบที่คนยืนยัน/แก้ไปแล้ว
จะไม่ถูกเขียนทับ — อัปเดตแค่รูป processed ให้เห็นภาพที่ถูกต้องตอนย้อนดู
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

import psycopg

from .config import DB_SCHEMA
from .db import build_row, connect, get_image, replace_image
from .imageio import encode_jpeg
from .preprocess import preprocess, upright
from .review import evaluate
from .web.pipeline import VARIANT, build_raw_ocr, count_duplicates, read_with_fallback

# ถือว่า crop เดิมพัง ถ้าขนาดต่างจาก crop ใหม่เกินเท่านี้ (สัดส่วนของด้าน)
SIZE_TOLERANCE = 0.02


@dataclass
class Candidate:
    slip_id: str
    old_size: tuple[int, int]
    new_size: tuple[int, int]
    quad_found: bool | None   # None = ยังไม่ได้ crop ดู เพราะรู้อยู่แล้วว่าต้องทำใหม่
    locked: bool          # คนยืนยัน/แก้ข้อมูลใบนี้แล้ว — ห้ามเขียนทับข้อมูล
    lock_reason: str
    why: str              # เหตุที่ต้องทำใหม่: crop เปลี่ยน / ยังไม่เคยเช็คว่ากลับหัว


def _same_size(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return all(abs(x - y) <= SIZE_TOLERANCE * max(x, y, 1) for x, y in zip(a, b))


def find_candidates(conn: psycopg.Connection) -> list[Candidate]:
    """ใบที่ควรทำใหม่ — crop ใหม่ได้ภาพต่างจากเดิม หรือยังไม่เคยผ่านการเช็คที่เพิ่มมาทีหลัง

    ทั้ง "ใบกลับหัวไหม" และ "crop ครอบตัวใบครบไหม" ดูจากขนาดภาพไม่ออก ต้องให้ model
    ดูถึงจะรู้ ใบที่เข้าระบบก่อนมีการเช็คพวกนี้จึงต้องยิงซ้ำ แม้ crop จะออกมาเท่าเดิมเป๊ะ
    """
    rows = conn.execute(
        f"""SELECT s.id::text AS id, s.review_status,
                   (s.raw_ocr ?& array['orientation', 'fills_frame']) AS all_checked,
                   (SELECT count(*) FROM {DB_SCHEMA}.slip_edits e WHERE e.slip_id = s.id) AS edits,
                   i.width, i.height
            FROM {DB_SCHEMA}.slips s
            JOIN {DB_SCHEMA}.slip_images i ON i.slip_id = s.id AND i.kind = 'processed'
            ORDER BY s.created_at"""
    ).fetchall()

    out: list[Candidate] = []
    for r in rows:
        old = (r["width"], r["height"])

        # ใบที่ยังไม่เคยผ่านการเช็คใหม่ เป็น candidate แน่นอนอยู่แล้ว ไม่ต้องโหลดรูปมาดู
        # — ตอนที่ยังโหลดทุกใบ การสแกนดึงรูปเป็น GB ข้ามเน็ตจน connection หลุดก่อนได้เริ่มทำงาน
        if not r["all_checked"]:
            why, new, quad_found = "ยังไม่เคยเช็คกลับหัว/crop ครอบครบ", old, None
        else:
            original = get_image(conn, r["id"], "original")
            if original is None:
                continue
            pre = preprocess(original)
            new, quad_found = pre.cropped.size, pre.quad_found
            if _same_size(old, new):
                continue
            why = "crop เปลี่ยน"

        locked = r["review_status"] != "pending" or r["edits"] > 0
        reason = "คนยืนยันแล้ว" if r["review_status"] != "pending" else (
            "คนแก้ข้อมูลแล้ว" if r["edits"] else ""
        )
        out.append(Candidate(r["id"], old, new, quad_found, locked, reason, why))
    return out


def reprocess_one(conn: psycopg.Connection, cand: Candidate) -> dict[str, Any]:
    """crop ใหม่ + หมุนถ้ากลับหัว + (ถ้าไม่ล็อก) เขียนทับข้อมูลที่ model เคยอ่านผิด"""
    original = get_image(conn, cand.slip_id, "original")
    pre = preprocess(original)

    # ต้องยิง model แม้กับใบที่คนยืนยันแล้ว เพราะเป็นทางเดียวที่รู้ว่าใบกลับหัวหรือไม่
    # แต่ของใบพวกนั้นจะใช้แค่ orientation ไม่แตะข้อมูลที่คนยืนยันไว้
    res, used, full_frame = read_with_fallback(pre)
    if not res.ok:
        replace_image(conn, cand.slip_id, "processed", encode_jpeg(pre.cropped), pre.cropped.size)
        return {"id": cand.slip_id, "ocr": False, "note": f"OCR ล้มเหลว: {res.error}"}

    cropped = upright(used, res.orientation)
    flipped = cropped is not used
    replace_image(conn, cand.slip_id, "processed", encode_jpeg(cropped), cropped.size)

    if cand.locked:
        # บันทึกไว้ว่าเช็ค orientation ของใบนี้แล้ว ไม่งั้นรอบหน้าจะถูกหยิบมาทำซ้ำไม่จบ
        conn.execute(
            f"""UPDATE {DB_SCHEMA}.slips
                SET raw_ocr = raw_ocr || %s::jsonb WHERE id = %s""",
            (json.dumps({"orientation": res.orientation, "fills_frame": res.fills_frame}),
             cand.slip_id),
        )
        return {"id": cand.slip_id, "ocr": False, "flipped": flipped, "full_frame": full_frame,
                "note": cand.lock_reason, "cost": float((res.usage or {}).get("cost") or 0)}

    fields = dict(res.fields)
    reasons, problems = evaluate(fields, res.confidence, count_duplicates(conn, fields))

    row = build_row(fields)
    row.update(
        id=cand.slip_id,
        needs_review=bool(reasons),
        review_reason=reasons,
        ocr_model=res.model,
        ocr_variant=VARIANT,
        ocr_confidence=json.dumps(res.confidence, ensure_ascii=False),
        ocr_cost_usd=(res.usage or {}).get("cost") or 0,
        ocr_tokens_in=(res.usage or {}).get("prompt_tokens") or 0,
        ocr_tokens_out=(res.usage or {}).get("completion_tokens") or 0,
        ocr_latency_s=round(res.latency_s, 2),
        raw_ocr=json.dumps(
            build_raw_ocr(res, pre.quad_found and not full_frame, problems, reprocessed=True),
            ensure_ascii=False,
        ),
    )
    sets = ", ".join(f"{c} = %({c})s" for c in row if c != "id")
    conn.execute(f"UPDATE {DB_SCHEMA}.slips SET {sets} WHERE id = %(id)s", row)
    return {"id": cand.slip_id, "ocr": True, "fields": fields, "reasons": reasons,
            "flipped": flipped, "full_frame": full_frame, "cost": float(row["ocr_cost_usd"])}


def main() -> None:
    ap = argparse.ArgumentParser(description="crop รูปที่เก็บไว้ใหม่ + OCR ซ้ำใบที่ยังไม่ยืนยัน")
    ap.add_argument("--apply", action="store_true", help="เขียนลงฐานข้อมูลจริง (ไม่ใส่ = ดูอย่างเดียว)")
    ap.add_argument("--limit", type=int, default=0, help="จำกัดจำนวนใบที่แก้ (0 = ไม่จำกัด)")
    args = ap.parse_args()

    conn = connect()
    try:
        cands = find_candidates(conn)
        if args.limit:
            cands = cands[: args.limit]

        locked = [c for c in cands if c.locked]
        recrop = [c for c in cands if c.why == "crop เปลี่ยน"]
        print(f"ใบที่ต้องทำใหม่: {len(cands)} ใบ "
              f"(crop เปลี่ยน {len(recrop)} ใบ, เช็คเพิ่มอย่างเดียว {len(cands) - len(recrop)} ใบ) "
              f"— เขียนทับข้อมูลได้ {len(cands) - len(locked)} ใบ, "
              f"อีก {len(locked)} ใบแก้แค่รูปเพราะมีคนยืนยัน/แก้แล้ว")
        for c in cands:
            note = f"  [{c.lock_reason}]" if c.locked else ""
            size = (f"{c.old_size[0]}x{c.old_size[1]} -> {c.new_size[0]}x{c.new_size[1]}"
                    if c.why == "crop เปลี่ยน" else f"{c.old_size[0]}x{c.old_size[1]}")
            print(f"  {c.slip_id[:8]} {size}  {c.why}{note}")

        if not args.apply:
            print("\n(ดูอย่างเดียว — ใส่ --apply เพื่อลงมือจริง)")
            return

        cost = 0.0
        for i, c in enumerate(cands, 1):
            try:
                r = reprocess_one(conn, c)
                conn.commit()
            except psycopg.OperationalError as exc:
                # Postgres ฝั่ง Render ตัด connection เป็นระยะเมื่อรันยาว ๆ
                # ต่อใหม่แล้วไปต่อใบถัดไป ใบที่ค้างจะถูกหยิบมาทำในรอบหน้าเอง
                print(f"[{i}/{len(cands)}] {c.slip_id[:8]} connection หลุด ({exc}) — ต่อใหม่")
                conn = connect()
                continue
            cost += r.get("cost", 0.0)
            flip = " [หมุนกลับหัว 180]" if r.get("flipped") else ""
            flip += " [crop พัง ใช้ภาพเต็มแทน]" if r.get("full_frame") else ""
            print(f"[{i}/{len(cands)}] {c.slip_id[:8]}{flip} "
                  + ("OCR ซ้ำแล้ว " + json.dumps(r["fields"], ensure_ascii=False)
                     if r["ocr"] else "อัปเดตรูปอย่างเดียว — " + r["note"]))
        print(f"\nเสร็จแล้ว ค่า OCR รวม ${cost:.4f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
