"""crop รูปที่เก็บไว้ใหม่ แล้ว OCR ซ้ำเฉพาะใบที่ยังไม่มีคนยืนยัน

ใช้หลังแก้ตัวจับขอบกระดาษ: ใบที่ถ่ายบนพื้นหลังขาวเคยถูก crop ไปโดนพื้นหลังแทนตัวใบ
ภาพที่ส่งเข้า model จึงเอียง/หมุน/ขอบขาด และอ่านผิดโดยไม่มีสัญญาณเตือน

    python -m ocrslip.reprocess            # ดูว่าจะกระทบใบไหนบ้าง (ไม่เขียนอะไร)
    python -m ocrslip.reprocess --apply    # ลงมือแก้จริง

กฎความปลอดภัย: รูป original ไม่ถูกแตะต้องเลย และข้อมูลของใบที่คนยืนยัน/แก้ไปแล้ว
จะไม่ถูกเขียนทับ — อัปเดตแค่รูป processed ให้เห็นภาพที่ถูกต้องตอนย้อนดู
"""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import dataclass
from typing import Any

import psycopg
from PIL import Image

from .config import DB_SCHEMA, OCR_MODEL
from .db import build_row, connect, get_image, replace_image
from .imageio import encode_jpeg
from .ocr import read_slip
from .preprocess import preprocess
from .review import evaluate
from .web.pipeline import VARIANT, count_duplicates

# ถือว่า crop เดิมพัง ถ้าขนาดต่างจาก crop ใหม่เกินเท่านี้ (สัดส่วนของด้าน)
SIZE_TOLERANCE = 0.02


@dataclass
class Candidate:
    slip_id: str
    old_size: tuple[int, int]
    new_size: tuple[int, int]
    quad_found: bool
    locked: bool          # คนยืนยัน/แก้ข้อมูลใบนี้แล้ว — ห้ามเขียนทับข้อมูล
    lock_reason: str


def _same_size(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return all(abs(x - y) <= SIZE_TOLERANCE * max(x, y, 1) for x, y in zip(a, b))


def find_candidates(conn: psycopg.Connection) -> list[Candidate]:
    """ใบที่ crop ใหม่แล้วได้ภาพต่างจากเดิม = ใบที่ได้ประโยชน์จากการแก้"""
    rows = conn.execute(
        f"""SELECT s.id::text AS id, s.review_status,
                   (SELECT count(*) FROM {DB_SCHEMA}.slip_edits e WHERE e.slip_id = s.id) AS edits,
                   i.width, i.height
            FROM {DB_SCHEMA}.slips s
            JOIN {DB_SCHEMA}.slip_images i ON i.slip_id = s.id AND i.kind = 'processed'
            ORDER BY s.created_at"""
    ).fetchall()

    out: list[Candidate] = []
    for r in rows:
        original = get_image(conn, r["id"], "original")
        if original is None:
            continue
        pre = preprocess(original)
        old, new = (r["width"], r["height"]), pre.cropped.size
        if _same_size(old, new):
            continue

        locked = r["review_status"] != "pending" or r["edits"] > 0
        reason = "คนยืนยันแล้ว" if r["review_status"] != "pending" else (
            "คนแก้ข้อมูลแล้ว" if r["edits"] else ""
        )
        out.append(Candidate(r["id"], old, new, pre.quad_found, locked, reason))
    return out


def reprocess_one(conn: psycopg.Connection, cand: Candidate) -> dict[str, Any]:
    """crop ใหม่ + (ถ้าไม่ล็อก) OCR ซ้ำแล้วเขียนทับข้อมูลที่ model เคยอ่านผิด"""
    original = get_image(conn, cand.slip_id, "original")
    pre = preprocess(original)
    processed = encode_jpeg(pre.cropped)
    replace_image(conn, cand.slip_id, "processed", processed, pre.cropped.size)

    if cand.locked:
        return {"id": cand.slip_id, "ocr": False, "note": cand.lock_reason}

    res = read_slip(processed, OCR_MODEL)
    if not res.ok:
        return {"id": cand.slip_id, "ocr": False, "note": f"OCR ล้มเหลว: {res.error}"}

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
            {"fields": res.fields, "quad_found": pre.quad_found,
             "problems": problems, "reprocessed": True},
            ensure_ascii=False,
        ),
    )
    sets = ", ".join(f"{c} = %({c})s" for c in row if c != "id")
    conn.execute(f"UPDATE {DB_SCHEMA}.slips SET {sets} WHERE id = %(id)s", row)
    return {"id": cand.slip_id, "ocr": True, "fields": fields, "reasons": reasons,
            "cost": float(row["ocr_cost_usd"])}


def main() -> None:
    ap = argparse.ArgumentParser(description="crop รูปที่เก็บไว้ใหม่ + OCR ซ้ำใบที่ยังไม่ยืนยัน")
    ap.add_argument("--apply", action="store_true", help="เขียนลงฐานข้อมูลจริง (ไม่ใส่ = ดูอย่างเดียว)")
    ap.add_argument("--limit", type=int, default=0, help="จำกัดจำนวนใบที่แก้ (0 = ไม่จำกัด)")
    args = ap.parse_args()

    with connect() as conn:
        cands = find_candidates(conn)
        if args.limit:
            cands = cands[: args.limit]

        locked = [c for c in cands if c.locked]
        print(f"ใบที่ crop ใหม่แล้วได้ภาพต่างจากเดิม: {len(cands)} ใบ "
              f"(อัปเดตรูป {len(cands)} ใบ, OCR ซ้ำ {len(cands) - len(locked)} ใบ, "
              f"ข้ามข้อมูล {len(locked)} ใบเพราะมีคนยืนยัน/แก้แล้ว)")
        for c in cands:
            note = f"  [{c.lock_reason}]" if c.locked else ""
            print(f"  {c.slip_id[:8]} {c.old_size[0]}x{c.old_size[1]} -> "
                  f"{c.new_size[0]}x{c.new_size[1]} quad={c.quad_found}{note}")

        if not args.apply:
            print("\n(ดูอย่างเดียว — ใส่ --apply เพื่อลงมือจริง)")
            return

        cost = 0.0
        for i, c in enumerate(cands, 1):
            r = reprocess_one(conn, c)
            conn.commit()
            cost += r.get("cost", 0.0)
            print(f"[{i}/{len(cands)}] {c.slip_id[:8]} "
                  + ("OCR ซ้ำแล้ว " + json.dumps(r["fields"], ensure_ascii=False)
                     if r["ocr"] else "อัปเดตรูปอย่างเดียว — " + r["note"]))
        print(f"\nเสร็จแล้ว ค่า OCR รวม ${cost:.4f}")


if __name__ == "__main__":
    main()
