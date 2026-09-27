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
from .preprocess import PREPROCESS_VERSION, preprocess, upright
from .review import evaluate
from .web.pipeline import VARIANT, build_raw_ocr, count_duplicates, read_with_fallback

@dataclass
class Candidate:
    slip_id: str
    old_size: tuple[int, int]
    locked: bool          # คนยืนยัน/แก้ข้อมูลใบนี้แล้ว — ห้ามเขียนทับข้อมูล
    lock_reason: str
    why: str


def find_candidates(conn: psycopg.Connection) -> list[Candidate]:
    """ใบที่ยังไม่ได้ผ่าน preprocess เวอร์ชันปัจจุบัน — ตัดสินด้วย SQL ล้วน ๆ

    เดิมตัดสินด้วยการ crop ใหม่ทุกใบแล้วดูว่าขนาดเปลี่ยนไหม ซึ่งต้องดึงรูปต้นฉบับ
    ทั้งฐานข้อมูลข้ามเน็ตมา (ระดับ GB) จน Postgres ตัด connection ทิ้งก่อนได้เริ่มทำงาน
    การ stamp เลขเวอร์ชันไว้ในแต่ละใบทำให้รู้คำตอบเดียวกันโดยไม่ต้องแตะรูปเลย
    """
    rows = conn.execute(
        f"""SELECT s.id::text AS id, s.review_status,
                   COALESCE((s.raw_ocr->>'preprocess_version')::int, 0) AS version,
                   (SELECT count(*) FROM {DB_SCHEMA}.slip_edits e WHERE e.slip_id = s.id) AS edits,
                   i.width, i.height
            FROM {DB_SCHEMA}.slips s
            JOIN {DB_SCHEMA}.slip_images i ON i.slip_id = s.id AND i.kind = 'processed'
            WHERE COALESCE((s.raw_ocr->>'preprocess_version')::int, 0) <> %s
            ORDER BY s.created_at""",
        (PREPROCESS_VERSION,),
    ).fetchall()

    out: list[Candidate] = []
    for r in rows:
        locked = r["review_status"] != "pending" or r["edits"] > 0
        reason = "คนยืนยันแล้ว" if r["review_status"] != "pending" else (
            "คนแก้ข้อมูลแล้ว" if r["edits"] else ""
        )
        out.append(Candidate(
            r["id"], (r["width"], r["height"]), locked, reason,
            f"ยังเป็น preprocess v{r['version']} (ตอนนี้ v{PREPROCESS_VERSION})",
        ))
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
            (json.dumps({"orientation": res.orientation, "fills_frame": res.fills_frame,
                         "preprocess_version": PREPROCESS_VERSION}), cand.slip_id),
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
        print(f"ใบที่ต้องทำใหม่: {len(cands)} ใบ — เขียนทับข้อมูลได้ {len(cands) - len(locked)} ใบ, "
              f"อีก {len(locked)} ใบแก้แค่รูปเพราะมีคนยืนยัน/แก้แล้ว")
        for c in cands[:20]:
            note = f"  [{c.lock_reason}]" if c.locked else ""
            print(f"  {c.slip_id[:8]} {c.old_size[0]}x{c.old_size[1]}  {c.why}{note}")
        if len(cands) > 20:
            print(f"  ... และอีก {len(cands) - 20} ใบ")

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
