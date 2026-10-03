"""Pull slips that were "confirmed while cropping was still broken" back into the review queue.

While the paper-edge detector was still broken, reviewers were looking at images that
were skewed, rotated or clipped, so they may have confirmed wrong data without realising
it. A real example found in production: one slip had stored the plate and the brand from
two different cars, even though both were written clearly in the photo.

The test: have the model read *two different views* of the same slip (the cropped image
and the full frame). Only when both views agree with each other, yet differ sharply from
the stored value, is the slip pulled back for a second human look.

The views have to be genuinely different images. Reading the same image twice proves
nothing at all, because at temperature=0 the same image yields the same answer.

A limitation worth stating plainly: the model agreeing across two views does not always
mean the human-entered value is wrong — genuinely ambiguous handwriting can be misread
the same way from both views. So this script never overwrites data. All it does is set
the status back to pending with the reason recheck_bad_crop, leaving the decision to a
reviewer now looking at a correct image.

    python -m ocrslip.recheck            # show which slips would be pulled back
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

# The fields this decision rests on: the ones actually used to find a car again. Wrong
# here means the car cannot be found.
CHECKED = ("name", "tel", "noplate")
AGREE_MAX = 0.15        # the two views may differ by at most this much to count as "agreeing"
CONFLICT_MIN = 0.5      # must differ from the stored value by more than this to count as a real conflict


def _cer(truth: str, got: str) -> float:
    if not truth:
        return 0.0 if not got else 1.0
    return min(1.0, Levenshtein.distance(truth, got) / len(truth))


def conflicts(stored: dict[str, Any], reads: list[dict[str, Any]]) -> list[str]:
    """Fields where every view read the same value, yet that value conflicts with what is stored.

    reads must come from genuinely different images of the same slip, not the same image
    read twice.
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
        # Two genuinely independent views: the cropped image, and the whole frame
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
    """Set the status back to pending with a reason. Not one field value is touched."""
    conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips
            SET review_status = 'pending', needs_review = true,
                review_reason = (SELECT array_agg(DISTINCT r)
                                 FROM unnest(array_append(review_reason,
                                                          'recheck_bad_crop')) r),
                reviewed_by = NULL, reviewed_at = NULL,
                -- A slip pulled back into the queue must not carry its old claim with
                -- it, or it stays held by someone who is no longer at their desk and gets
                -- handed to nobody until the claim expires.
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
