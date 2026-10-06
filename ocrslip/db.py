"""Postgres connection plus the queries the web app actually runs.

Create the schema with:  python -m ocrslip.db init
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
    # -c TimeZone has to be set here, not converted at render time. The now() written
    # into returned_at and reviewed_at, and the interval behind a queue claim, all resolve
    # against this session's timezone. Convert only at display time and something always
    # slips through — the case that caught us: a slip closed at 16:15 shown as 09:15.
    return psycopg.connect(
        DATABASE_URL, row_factory=dict_row, options=f"-c TimeZone={APP_TIMEZONE}")


def split_sql(sql: str) -> list[str]:
    """Split schema.sql into statements without cutting through a string, comment or $$...$$ block.

    It has to understand dollar-quoting, because the body of touch_updated_at() contains
    a ";" — splitting naively on ";" would cut the function in half and produce a syntax
    error.
    """
    stmts, buf = [], []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):           # end-of-line comment
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "'":                                        # ordinary string
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
        if ch == "$":                                        # dollar-quote: $$ or $tag$
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
    """Create or update the schema one statement at a time, committing each one.

    This used to run the whole file in a single transaction, which failed completely if
    the connection dropped partway through (Postgres on Render cuts the line periodically)
    and rolled back everything already done. Every statement in the file is written to be
    re-runnable (IF NOT EXISTS / CREATE OR REPLACE), so reconnecting and retrying the same
    statement is safe.
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
                    print(f"[{idx}/{len(stmts)}] connection dropped ({exc}) — reconnecting and retrying")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = connect()
            if verbose:
                print(f"[{idx}/{len(stmts)}] {' '.join(stmt.split())[:80]}")
    finally:
        conn.close()


# ---------- writes ----------

def build_row(fields: dict[str, Any]) -> dict[str, Any]:
    """Turn human-confirmed values into table columns, computing the *_norm columns used for search"""
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
        # With no photographer given, assume it was the same person who uploaded
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
    """Store this slip's evidence image — deduplicated within a single slip only.

    It must not deduplicate across slips: otherwise re-uploading the same photo produces
    a record with no evidence image attached to it.
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
    """Replace this slip's image with a new file (used when reprocessing a bad crop).

    The old one is deleted first so no stale image is left behind, because get_image picks
    the oldest image of that kind.
    """
    conn.execute(
        f"DELETE FROM {DB_SCHEMA}.slip_images WHERE slip_id = %s AND kind = %s", (slip_id, kind)
    )
    add_image(conn, slip_id, kind, jpeg, size)


def slip_with_image(conn: psycopg.Connection, jpeg: bytes) -> dict[str, Any] | None:
    """The slip already holding this image (byte-identical). None = this image is new.

    Returns the *slip* rather than True/False, because whoever re-uploaded needs to be
    told which slip it already is and what state that slip is in. Always the oldest one,
    so that where duplicates already exist everyone points at the same slip rather than
    forming a chain of pointers.
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
    """Save human edits and write an audit log for the fields that actually changed. Returns how many changed.

    require_status = the status the slip must be in at write time (an optimistic lock).
    Returns None when the status does not match, which means somebody else reviewed this
    slip while this page sat open. Nothing at all must be written in that case, or the
    first person's work is silently overwritten along with a duplicate set of audit rows.
    """
    before = get_slip(conn, slip_id)
    row = build_row(fields)
    if review_status is not None:
        row["review_status"] = review_status
        row["needs_review"] = bool(review_reason)
        row["review_reason"] = review_reason or []
        row["reviewed_by"] = edited_by
        row["reviewed_at"] = "now()"
        # A slip leaving the pending pile need not be held by anyone; release it in the same statement
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
    """Close the slip as car-returned. Returns False if it was already closed (never overwrite whoever got there first).

    The car_status = 'stored' condition has to live *inside* the UPDATE rather than being
    checked before writing: at the checkout point several staff work from separate screens,
    and pressing the same slip simultaneously genuinely happens. Allowing the overwrite
    would make the handover name and time belong to whoever pressed last, erasing the
    record of the person who actually released the car. (The same problem that was fixed
    at the approval step for concurrent reviewers.)
    """
    cur = conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips
            SET car_status = 'returned', returned_at = now(), returned_by = %s,
                returned_note = %s, released_to = %s
            WHERE id = %s AND car_status = 'stored'""",
        (by, note, released_to, slip_id),
    )
    return cur.rowcount == 1


def add_car_check(
    conn: psycopg.Connection, slip_id: str, tel: str, plate_raw: str, reason: str,
    by: str | None,
) -> dict[str, Any]:
    """Record a visit to a car that stays parked (started it, checked it, took something out).

    The slip itself is left untouched: car_status stays 'stored', because the car is still here.
    """
    return conn.execute(
        f"""INSERT INTO {DB_SCHEMA}.car_checks (slip_id, tel, plate_raw, reason, checked_by)
            VALUES (%s, %s, %s, %s, %s) RETURNING *""",
        (slip_id, tel, plate_raw, reason, by),
    ).fetchone()


def open_car_check(conn: psycopg.Connection, slip_ids: list[str]) -> dict[str, Any] | None:
    """The visit still in progress for any of these slips (checked in, not yet checked out), if any"""
    if not slip_ids:
        return None
    return conn.execute(
        f"""SELECT * FROM {DB_SCHEMA}.car_checks
            WHERE slip_id = ANY(%s::uuid[]) AND finished_at IS NULL
            ORDER BY created_at DESC LIMIT 1""",
        (slip_ids,),
    ).fetchone()


def finish_car_checks(conn: psycopg.Connection, slip_id: str, by: str | None) -> list[dict[str, Any]]:
    """Close every visit still open on this slip. Returns the rows closed (empty = nothing was open).

    finished_at IS NULL lives inside the UPDATE, as in mark_returned, so two staff pressing at once
    cannot overwrite the first one's time.
    """
    return conn.execute(
        f"""UPDATE {DB_SCHEMA}.car_checks SET finished_at = now(), finished_by = %s
            WHERE slip_id = %s AND finished_at IS NULL RETURNING *""",
        (by, slip_id),
    ).fetchall()


def list_car_checks(conn: psycopg.Connection, slip_id: str) -> list[dict[str, Any]]:
    """Every visit to this slip's car, newest first"""
    return conn.execute(
        f"""SELECT * FROM {DB_SCHEMA}.car_checks WHERE slip_id = %s ORDER BY created_at DESC""",
        (slip_id,),
    ).fetchall()


def norm_loc(alias: str) -> str:
    """SQL expression comparing parking spots ignoring whitespace and case ("อาคาร 1  ชั้น 2" = "อาคาร 1 ชั้น 2").

    OCR routinely returns different spacing from two photos of the same paper slip.
    Compared literally, a genuine duplicate escapes detection over nothing but a space.
    """
    return f"lower(btrim(regexp_replace(coalesce({alias}.location, ''), '\\s+', ' ', 'g')))"


# Two kinds of duplicate, which must be kept apart:
#   * matching original-image hash = the same file was submitted twice. 100% certain, so it
#     can be pulled straight out of the queue.
#   * matching plate + phone + date + parking spot = one paper slip photographed twice
#     (from different angles, hence different hashes).
#
# The second kind is inferred from the slip's own values, so it needs two further layers to
# keep a genuine *new parking round* from being swept up:
#   1. The deposit date must match — keeps out the same car parked again next month.
#   2. The parking spot must match — keeps out a new round *on the same day* (parked in the
#      morning, collected at midday, parked again that evening), which layer 1 cannot catch
#      at all, since plate, phone and date all agree even though these are separate rounds.
#      A new round always gets a new bay, while two photos of one paper slip necessarily
#      carry the same spot.
#   3. Both must still be stored — a returned slip has closed its own round, so the next one
#      is a new round. (This layer only helps when the paperwork trails the real world; it
#      is not the main gate.)
#
# The two ways of being wrong do not cost the same. Guess "not a duplicate" wrongly and a
# reviewer loses the time it takes to process one extra slip. Guess "duplicate" wrongly and
# a car is genuinely parked with no active slip for it — which surfaces only when the owner
# arrives to collect and the slip cannot be found. So this condition set is deliberately
# biased toward leaving things in the queue.
def same_slip() -> str:
    """SQL condition for "slip d and slip k are the same slip".

    This has to be a function, not a module-level constant: an f-string assembled at import
    time bakes in whatever the schema name was then, so when the tests point at a different
    schema the statement still runs against ocr_dhammakaya. (The same bug reject_slip once
    had.)
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
    """Reject an unusable slip (blurred photo, not a parking slip at all) without touching its data.

    The SQL belongs here rather than in the route: it used to carry a hardcoded schema name
    in main.py, so the statement always landed on ocr_dhammakaya whatever DB_SCHEMA was set
    to. Running the tests against another schema therefore made rejection vanish silently
    (0 rows) while still answering 303 as though it had succeeded.
    """
    conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips
               SET review_status = 'rejected', needs_review = false, review_reason = %s,
                   reviewed_by = %s, reviewed_at = now(),
                   -- A slip leaving the pending pile need not be held by anyone
                   claimed_by = NULL, claimed_name = NULL, claimed_at = NULL
             WHERE id = %s""",
        ([reason], reviewer, slip_id),
    )


def mark_superseded(conn: psycopg.Connection, keeper_id: str) -> int:
    """Pull still-queued slips that are the same slip as keeper out of the queue. Returns how many.

    Called immediately after approval — that is the one moment where we know for certain
    that "this slip has been reviewed", which makes the remaining identical ones duplicates
    nobody needs to review again.

    Only pending slips are touched; already-reviewed slips (approved or rejected alike) are
    left alone, because work a human has done must not be buried by a script's inference.
    Where duplicates have already been approved, an admin decides what to delete, from the
    report (python -m ocrslip.dedup).
    """
    cur = conn.execute(
        f"""UPDATE {DB_SCHEMA}.slips d
               SET superseded_by = k.id, superseded_at = now(),
                   -- A slip leaving the queue need not be held by anyone
                   claimed_by = NULL, claimed_name = NULL, claimed_at = NULL
              FROM {DB_SCHEMA}.slips k
             WHERE k.id = %(keep)s AND d.id <> k.id
               AND d.review_status = 'pending' AND d.superseded_by IS NULL
               AND {same_slip()}""",
        {"keep": keeper_id},
    )
    return cur.rowcount


def delete_slip(conn: psycopg.Connection, slip_id: str) -> dict[str, Any] | None:
    """Delete a slip permanently. Returns the deleted row (None if it was already gone).

    Its evidence images and edit history go with it via ON DELETE CASCADE, which is what we
    want: what gets deleted is junk (test slips, nonsense entries, repeated submissions),
    and keeping the remains around only skews the summary figures.

    The deleted row is returned via DELETE ... RETURNING rather than SELECT-then-DELETE, so
    that what reaches the log is the row that was genuinely removed, not the row as it read
    a moment earlier.
    """
    cur = conn.execute(
        f"""DELETE FROM {DB_SCHEMA}.slips WHERE id = %s
            RETURNING id::text AS id, name, tel, plate_raw, car_status, entry_source""",
        (slip_id,),
    )
    return cur.fetchone()

# ---------- settings editable from the web UI ----------

def get_settings(conn: psycopg.Connection) -> dict[str, str]:
    """Every setting ever saved from the web UI (keys never set are simply absent)"""
    cur = conn.execute(f"SELECT key, value FROM {DB_SCHEMA}.app_settings")
    return {r["key"]: r["value"] for r in cur.fetchall()}


def set_setting(conn: psycopg.Connection, key: str, value: str, by: str | None = None) -> None:
    """Save a setting. An empty value means "stop setting this from the web UI", so the row is deleted and the env value takes over.

    It has to delete rather than store an empty string, otherwise clearing the field on the
    settings page becomes setting it *to* empty, overriding the env value instead of falling
    back to the default.
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

# ---------- reads ----------

def open_slip_by_plate(
    conn: psycopg.Connection, plate_norm: str, *, hours: int = 12
) -> dict[str, Any] | None:
    """This plate's slip that has not been collected yet and was registered recently.

    Guards against a repeated entry-form submission (page refresh, double-tap) giving one
    car two slips. A duplicate does no damage at save time; it does damage on the way out,
    where staff see two identical rows and cannot tell which one to close. Close the wrong
    one and a slip is left stranded that nobody will ever come to collect.
    """
    cur = conn.execute(
        f"""SELECT * FROM {DB_SCHEMA}.slips
            WHERE plate_norm = %s AND car_status = 'stored'
              AND created_at > now() - (%s || ' hours')::interval
            ORDER BY created_at DESC LIMIT 1""",
        (plate_norm, hours),
    )
    return cur.fetchone()


def slips_by_plate(conn: psycopg.Connection, plate_norm: str) -> list[dict[str, Any]]:
    """Every slip that counts as real for this plate, newest first — used by the exit form (/out).

    It must return *both* still-parked and already-closed slips rather than filtering down
    to the parked ones: somebody who has already driven away but submits the form again,
    and somebody who mistyped their plate, need different messages ("this slip was
    collected at ..." versus "no slip found for this plate"), because the remedies are
    different. Given the same message, the first person stands there re-entering their
    details forever, never learning the car has already been released.

    Slips marked as duplicates (superseded_by) or rejected are excluded, since neither is
    a real deposit.
    """
    if not plate_norm:
        return []
    cur = conn.execute(
        f"""SELECT * FROM {DB_SCHEMA}.slips
            WHERE plate_norm = %s AND superseded_by IS NULL AND review_status <> 'rejected'
            ORDER BY created_at DESC""",
        (plate_norm,),
    )
    return cur.fetchall()


def _rounds_base(*, one: bool = False) -> str:
    """Slips that count as real deposits, plus the key identifying a "round" (the deposit date).

    A slip with no deposit date cannot be grouped with anything, so it becomes its own
    round (the key falls back to its id). Letting the NULLs group together would flag every
    slip with an unreadable date as a duplicate of every other.
    """
    return f"""SELECT id, plate_norm, deposit_date, location, car_status,
                      review_status, returned_at, created_at,
                      coalesce(deposit_date::text, 'id:' || id::text) AS gkey
                 FROM {DB_SCHEMA}.slips
                WHERE plate_norm {'= %s' if one else '= ANY(%s)'}
                  AND superseded_by IS NULL AND review_status <> 'rejected'"""


def deposit_rounds(
    conn: psycopg.Connection, plate_norms: list[str]
) -> dict[str, dict[str, Any]]:
    """Plates with more than one slip -> {slip_id: {"round": n, "total": m, "dup": k}}

    A group of people parked, collected, and parked again — two or three rounds per car has
    been observed. The search page shows these as near-identical rows ordered by score, so
    exit staff were left reading the dates themselves to work out which slip is the current
    round. Close the wrong one and a slip is stranded that nobody will come to collect.

    A "round" counts by deposit date, not by slip. The same slip photographed two or three
    times (different hashes, the parking spot transcribed differently, or one of them
    already closed) can slip past the duplicate check at approval time. Counting by slip
    would turn three same-day duplicates into badges reading "round 1/2/3 of 3", which lies
    outright to staff about the car having been parked three times. So dup states plainly
    how many slips share that one day, and the web UI badges them "possible duplicate"
    rather than inventing a round number.

    Only slips with something worth saying are returned (multiple rounds, or same-day
    siblings): badging a lone slip "round 1 of 1" is pure clutter. Slips marked duplicate
    or rejected are not counted, since neither is a real deposit.
    """
    plates = [p for p in {p for p in plate_norms} if p]
    if not plates:
        return {}
    rows = conn.execute(
        f"""SELECT id::text AS id, round, total, dup FROM (
                SELECT id, plate_norm, round, dup,
                       max(round) OVER (PARTITION BY plate_norm) AS total
                  FROM (SELECT id, plate_norm,
                               dense_rank() OVER (PARTITION BY plate_norm
                                                  ORDER BY gkey) AS round,
                               count(*) OVER (PARTITION BY plate_norm, gkey) AS dup
                          FROM ({_rounds_base()}) b) r
            ) t WHERE total > 1 OR dup > 1""",
        (plates,),
    ).fetchall()
    return {r["id"]: {"round": r["round"], "total": r["total"], "dup": r["dup"]}
            for r in rows}


def deposit_history(conn: psycopg.Connection, plate_norm: str) -> list[dict[str, Any]]:
    """Every slip for this plate, ordered by round — shown on the slip page when releasing a car.

    Staff need to see at a glance which round the open slip belongs to, and whether the
    other rounds are all closed. Round numbers count by deposit date, as in
    deposit_rounds(), so two slips from the same day share a round number (i.e. they are
    duplicates) rather than becoming separate rounds. See deposit_rounds() for the full
    reasoning.
    """
    if not plate_norm:
        return []
    return conn.execute(
        f"""SELECT id::text AS id, round, dup, deposit_date, location, car_status,
                   review_status, returned_at
              FROM (SELECT b.*,
                           dense_rank() OVER (ORDER BY gkey) AS round,
                           count(*) OVER (PARTITION BY gkey) AS dup
                      FROM ({_rounds_base(one=True)}) b) t
             ORDER BY round, created_at""",
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
    """(id of the next slip in the queue, how many remain) — asked of the database directly.

    This used to pull the whole queue (SELECT * over 200 rows, OCR jsonb included) and sort
    it in Python just to get the first slip and a count — far too heavy for a page that
    loads on every single slip reviewed.

    Ordered so slips needing review come first, oldest first within that (clear the backlog
    before anything else).
    """
    # Two subqueries in one statement = a single round trip, without needing count(*) OVER (),
    # which would force a read of the entire sorted set (measured at 66,000 slips: 96 ms -> 25 ms)
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


# ---------- claiming slips in the queue ----------
#
# The problem this solves: next_in_queue() handed the same head-of-line slip to everyone,
# so three people reviewing at once always got the same slip and spent their time redoing
# each other's work.
#
# A claim is advisory, not a lock. People claim a slip and close the tab all the time; a
# hard lock would leave the queue full of untouchable slips. What guarantees data is not
# overwritten is still require_status in update_slip(). The claim only makes collisions
# rare; the write guard makes a collision harmless.

CLAIM_COLS = "claimed_by = NULL, claimed_name = NULL, claimed_at = NULL"


def release_claims(conn: psycopg.Connection, worker: str) -> None:
    """Release every slip this person holds — one person holds at most one slip at a time.

    Called before every new claim. Without it, somebody repeatedly pressing "skip" leaves
    the queue littered with claimed slips, and everyone else waits for those leases to
    expire even though nobody is actually reviewing them.
    """
    conn.execute(
        f"UPDATE {DB_SCHEMA}.slips SET {CLAIM_COLS}"
        f" WHERE claimed_by = %s AND review_status = 'pending'",
        (worker,),
    )


def claim_next(conn: psycopg.Connection, worker: str, name: str | None = None) -> str | None:
    """Release the current slip and claim the next in the queue. Returns the claimed id (None = queue empty).

    Both conditions have to be in the one statement, because they guard different cases:

    * claimed_at — excludes slips somebody else has claimed and committed (minute-scale collisions)
    * FOR UPDATE SKIP LOCKED — stops two simultaneous, uncommitted transactions fighting over
      the same row (millisecond-scale collisions). This one knows nothing about slips already
      claimed and committed, while claimed_at cannot see a transaction still in flight.
      Neither can be dropped.

    Ordered as next_in_queue was: slips needing review first, oldest first within that.
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
    """Claim a specific slip (somebody clicked it in the queue list). Returns None on success.

    On failure it returns the holder's row, to be shown on screen — a warning, not a block,
    because it may be the same person on a second device, or they may have deliberately
    come to look at this particular slip.
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
    """Release the other slips this person holds, leaving only the one currently open"""
    conn.execute(
        f"UPDATE {DB_SCHEMA}.slips SET {CLAIM_COLS}"
        f" WHERE claimed_by = %s AND id <> %s AND review_status = 'pending'",
        (worker, keep),
    )


def known_people(conn: psycopg.Connection) -> list[str]:
    """The active names, offered as choices at upload time"""
    cur = conn.execute(
        f"SELECT name FROM {DB_SCHEMA}.staff_members WHERE active ORDER BY name"
    )
    return [r["name"] for r in cur.fetchall()]


def list_staff(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Every name plus how many slips each person uploaded and reviewed (so an admin can see who did what).

    Uploading and reviewing are separate roles, so one row needs two separate counts.
    Implemented as a LEFT JOIN against totals grouped once, not a per-row subquery, which
    would scan the slips table once per name.
    Names are matched with lower(btrim()) to agree with build_filters, so the numbers on
    this page equal the number of rows you actually get when you click through.
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


# The value the form uses to mean "let me type a new name" must never make it through as a
# real person's name: it would show up as a duplicate option in the list, and anyone who
# picked it would be read as the sentinel forever after.
NEW_NAME_SENTINEL = "__new__"

# Invisible characters (zero-width, BOM) that .strip() cannot remove. Let them through and
# you get a name that reads as empty to a human while the system counts it as a value.
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff\u00ad]")


def clean_person_name(name: str | None) -> str:
    """A usable person name — invisible characters stripped, and the sentinel value rejected"""
    cleaned = _INVISIBLE.sub("", str(name or "")).strip()
    if cleaned == NEW_NAME_SENTINEL:
        return ""
    return cleaned


def add_staff(conn: psycopg.Connection, name: str, created_by: str | None = None) -> bool:
    """Add a name to the list. Returns False if it already exists (compared case- and trim-insensitively)."""
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
              -- The to-do pile must not count duplicates, or the tab badge disagrees with the rows shown
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
    """Rows for the Excel export — using the same filter set as the table page"""
    where, params = filters or ("TRUE", {})
    cur = conn.execute(
        f"""SELECT name, tel, plate_raw, province, brand, car_type, location, deposit_date,
                   review_status, car_status, returned_at, returned_by,
                   uploaded_by, photographer, reviewed_by, ocr_model, created_at
            FROM {DB_SCHEMA}.slips WHERE {where} ORDER BY created_at""",
        params,
    )
    return cur.fetchall()

# ---------- data table + dashboard ----------

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
    """Build the WHERE clause shared by the table page, the review queue, the counters and the Excel export.

    One implementation everywhere, so the "download Excel" button always returns exactly
    what is on screen.

    Searches run against the *_norm columns rather than the raw ones, because the trigram
    indexes live on *_norm. ILIKE against a raw column cannot use an index at all, which
    turns every search into a full table scan.
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
    # Duplicates have not left the system, they are just out of the to-do pile — hence a
    # filter, not permanent hiding.
    if superseded is not None:
        where.append(f"superseded_by IS {'NOT NULL' if superseded else 'NULL'}")
    # Filter by person, compared case- and trim-insensitively: a name typed by hand at
    # upload or review time may differ from the one in the list by nothing but whitespace,
    # which would make them two different people as far as the query is concerned.
    for key, val in (("uploaded_by", uploaded_by), ("reviewed_by", reviewed_by)):
        name = clean_person_name(val)
        if name:
            where.append(f"lower(btrim({key})) = lower(btrim(%({key})s))")
            params[key] = name
    if q and q.strip():
        # Normalize the query the same way the data was normalized on save, otherwise typing
        # "นายสมชาย" fails to find the row stored as "สมชาย".
        # Only include fields that still hold text after normalizing, or LIKE '%%' matches every row.
        parts = []
        for col, key, val in (
            ("name_norm", "qname", norm_name(q)),
            ("plate_norm", "qplate", norm_plate(q)),
            ("brand_norm", "qbrand", norm_brand(q)),
            ("location", "qloc", _base(q).strip()),   # parking spot has no norm column; trgm sits on the raw one
        ):
            if val:
                parts.append(f"{col} ILIKE %({key})s")
                params[key] = f"%{val}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        if digits:  # with no digits at all, the phone condition must be omitted, or LIKE '%%' matches every row
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
    """(rows for this page, total matching the filters) in a single round trip to the database.

    total may be passed in when the caller already knows it (the review queue knows it from
    review_counts), so nothing needs counting again.

    The database lives on a different machine from the web app: RTT measures 60-550 ms while
    the work on the Postgres side takes under 1 ms. So what costs time is the *number of
    round trips*, not how heavy the query is — which makes count(*) OVER () cheaper than a
    second COUNT(*) call, at the price of the window having to read every matching row.
    Measured on a synthetic dataset:

        slips      count(*) OVER ()    separate COUNT(*) (+1 RTT ~67 ms)
        1,700              1.3 ms                      ~68 ms
        33,000            96.5 ms                      ~92 ms
        200,000          408.0 ms                      ~75 ms

    The break-even point sits around 20,000-30,000 slips: below that the window wins, above
    it a separate COUNT does (production currently holds ~1,700). The review queue needs
    neither when nothing is being searched, because review_counts() already returned every
    pile's total in a round trip already made.

    SELECT * is avoided, because it drags raw_ocr and ocr_confidence (large jsonb blobs)
    along for nothing — neither the table page nor the review queue uses them, and over a
    real connection the difference measured several times over.
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
                   -- Computed in the database, because Postgres's clock is the one that
                   -- decides whether a claim has expired. Comparing on the Python side
                   -- would be comparing against a different clock.
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
            # The page is empty because we are past the last page; ask for the real count
            # so the pagination controls stay correct.
            total = conn.execute(
                f"SELECT count(*) AS n FROM {DB_SCHEMA}.slips WHERE {where}", params
            ).fetchone()["n"]
        else:
            total = 0
    return rows, total


def dashboard_stats(conn: psycopg.Connection) -> dict[str, Any]:
    """Every summary figure in a handful of queries — the dashboard has to stay light"""
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

    # The day axis has to be "every day in the last 30", not "the 14 days that happen to have
    # slips". The previous version strung the days that had slips together, so empty days
    # vanished from the axis, and dates whose year OCR misread (2083, 2027) were drawn as bars
    # next to 2026 with labels showing only DD/MM — which read as though they were in sequence.
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

    # Slips that fall outside the chart above must always be counted somewhere, or the chart
    # looks like the whole dataset when in fact hundreds sit off its edges (no date, an
    # impossible year, or a date older than 30 days).
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

    # OCR quality: which fields humans correct most often (taken from the production audit log)
    edits = conn.execute(
        f"""SELECT field AS label, count(*) AS n FROM {DB_SCHEMA}.slip_edits
            GROUP BY 1 ORDER BY n DESC LIMIT 8"""
    ).fetchall()
    # Who approved or rejected how many — a separate set from by_uploader, because uploading
    # and reviewing are not done by the same people. The "unattributed" group is included
    # (older slips reviewed before there was a reviewer-name field), otherwise the total does
    # not reconcile with approved.
    #
    # Grouped by lower(btrim()) as in build_filters, then mode() picks the most common
    # spelling as the display name. Otherwise "first" and "FIRST" become two rows on screen
    # that both lead to the same set of slips (i.e. the number on the row disagrees with what
    # you actually see).
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
            print(f"schema {DB_SCHEMA} ready:", [r["tablename"] for r in cur.fetchall()])
    else:
        print(__doc__)
