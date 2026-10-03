"""Report duplicate slips and pull queued duplicates out of the review queue.

Duplicates came from uploading the same batch of photos twice (the file picker was not
cleared after a successful upload, so anyone who thought it had not gone through
pressed again). The effect was that reviewers were handed slips a colleague had already
reviewed, as though the work had not been saved. The root cause is now closed at ingest;
this script exists to clean up what was already in flight.

Two kinds of duplicate are detected — the same two that db.same_slip() uses at approval
time:
  * matching original-image hash = the same file was submitted twice
  * matching plate + phone + deposit date + parking spot = one paper slip photographed
    twice (so the hashes differ). Both must also be un-returned; see db.same_slip() for
    the reasoning behind each condition.

--apply does exactly one thing: a slip that is still pending and has an already-approved
sibling is marked as a duplicate (superseded_by) and drops out of the queue. Nothing is
deleted, no field value is touched, and no reviewed slip is touched. Where both
duplicates are already approved, this script only reports them, because choosing which
one to discard means a person looking at the actual slips (deletable from the web UI at
/search -> open the slip -> delete).

    python -m ocrslip.dedup            # report only, writes nothing
    python -m ocrslip.dedup --apply    # actually pull queued duplicates out of the queue
    python -m ocrslip.dedup --limit 20 # cap how many groups are printed in detail
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
    """Parking spot, whitespace- and case-insensitive. The Python side of db.norm_loc(); the two must agree."""
    return re.sub(r"\s+", " ", (value or "")).strip().lower()


SLIP_COLS = """s.id::text AS id, s.name, s.tel, s.plate_raw, s.plate_norm, s.tel_digits,
                   s.province, s.brand, s.car_type, s.deposit_date, s.location,
                   s.review_status, s.reviewed_by, s.superseded_by, s.uploaded_by,
                   s.car_status, s.returned_at, s.created_at"""

# Prefilter for "slips eligible to be a duplicate of something". Its key is deliberately
# looser than group_duplicates' (it ignores parking spot and car status), because this is
# only a gate that drops the irrelevant before handing the rest to union-find. The real
# decision still lives in group_duplicates alone, and since the looser key is a superset
# of the real one, no member of a real group can be lost here. (Making it narrower would
# silently drop members from groups.)
# This has to be a JOIN, not "id IN (... OR ...)": the latter makes the planner walk
# EXISTS plus a row comparison over every row of the table — measured at 11 seconds,
# against under a second for the JOIN with UNION.
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
    """Every slip plus its original image hash (self-service slips have no photo, so NULL).

    only_candidates = restrict to slips eligible to be duplicates, used when the web UI
    calls this. The whole table (5,700 rows) takes 1.7 seconds over a real connection,
    too slow for a page that gets clicked through hundreds of times. The CLI script still
    loads the whole table, because it runs once and the full picture is the point.
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
    """Collect slips that are "the same slip" into one group; returns only groups larger than 1.

    Uses union-find because three slips can be linked along different edges (A and B share
    an image, B and C share a plate). Grouping by one key at a time would count the same
    group twice.
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
        # The key must match db.same_slip() exactly, otherwise the report describes
        # something different from what --apply does. (Date + parking spot + not yet
        # returned are the three layers that stop a fresh parking round being called a
        # duplicate.)
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
    """The group's canonical slip = the oldest approved one (None = nobody has reviewed any).

    It must be an approved slip. A group nobody has reviewed yet is left for a reviewer to
    handle normally — on approval the system pulls the rest out of the queue by itself.
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

        # Three piles, each handled differently
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
            # Every approved slip in the group has to be walked, not just the canonical
            # one: a group is held together by different keys, so a queued slip may be the
            # duplicate of some other approved slip in the group rather than the oldest
            # one. (mark_superseded skips slips already marked, so nothing is double
            # counted.)
            for k in sorted((s for s in g if s["review_status"] == "approved"),
                            key=lambda s: s["created_at"]):
                moved += mark_superseded(conn, k["id"])
        conn.commit()
        print(f"ถอนใบซ้ำออกจากคิวแล้ว {moved} ใบ — ข้อมูลกับรูปหลักฐานยังอยู่ครบ "
              f"ดูได้ที่คิวตรวจ กอง \"ซ้ำ\"")


if __name__ == "__main__":
    main()
