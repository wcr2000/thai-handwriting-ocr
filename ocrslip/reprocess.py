"""Re-crop the stored images, then re-run OCR only on slips nobody has confirmed yet.

Run this after fixing the paper-edge detector: older slips were cropped onto the
background or a blank sheet, or had their edges clipped, so the image sent to the model
was skewed, rotated or cut — and misread with no warning signal.

    python -m ocrslip.reprocess            # show which slips would be affected (writes nothing)
    python -m ocrslip.reprocess --apply    # actually make the changes

Safety rules: the original image is never touched, and data on slips a human has
confirmed or edited is never overwritten — only the processed image is updated, so that
looking back at the slip shows the correct picture.
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
    locked: bool          # a human has confirmed or edited this slip — its data must not be overwritten
    lock_reason: str
    why: str


def find_candidates(conn: psycopg.Connection) -> list[Candidate]:
    """Slips that have not been through the current preprocess version — decided in pure SQL.

    This used to be decided by re-cropping every slip and checking whether the size
    changed, which meant pulling every original image in the database across the network
    (gigabytes of it) — Postgres dropped the connection before the job even got going.
    Stamping the version number onto each slip answers the same question without touching
    a single image.
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
    """Re-crop, rotate if upside down, and (unless locked) overwrite data the model previously misread"""
    original = get_image(conn, cand.slip_id, "original")
    pre = preprocess(original)

    # The model has to be called even for human-confirmed slips, because it is the only
    # way to know whether the slip is upside down. For those slips only the orientation is
    # used; the confirmed data is left alone.
    res, used, full_frame = read_with_fallback(pre)
    if not res.ok:
        replace_image(conn, cand.slip_id, "processed", encode_jpeg(pre.cropped), pre.cropped.size)
        return {"id": cand.slip_id, "ocr": False, "note": f"OCR ล้มเหลว: {res.error}"}

    cropped = upright(used, res.orientation)
    flipped = cropped is not used
    replace_image(conn, cand.slip_id, "processed", encode_jpeg(cropped), cropped.size)

    if cand.locked:
        # Record that this slip's orientation was checked, or the next run picks it up
        # again, forever.
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
                # Postgres on Render drops the connection periodically on long runs.
                # Reconnect and move to the next slip; whatever was left is picked up on
                # the next run by itself.
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
