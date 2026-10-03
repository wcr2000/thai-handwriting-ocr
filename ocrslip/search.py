"""Fuzzy search: a misspelled name, a mistyped phone number or a misread plate still finds the closest slip.

Two layers: pull candidates out of Postgres with trigram/levenshtein (fast, index-backed),
then rerank in Python with rapidfuzz, which ranks better than raw similarity does.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
from rapidfuzz import fuzz

from .config import DB_SCHEMA
from .normalize import _base, norm_brand, norm_name, norm_phone, norm_plate

CANDIDATE_LIMIT = 150


def classify(query: str) -> str:
    """Guess what the user typed: a phone number, a plate, or a name"""
    q = _base(query)
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 6 and len(digits) >= len(q.replace(" ", "")) - 2:
        return "tel"
    if re.search(r"[ก-ฮ]", q) and re.search(r"\d", q) and len(q.replace(" ", "")) <= 10:
        return "plate"
    return "name"


def search(conn: psycopg.Connection, query: str, *, include_pending: bool = False,
           limit: int = 20) -> list[dict[str, Any]]:
    q = _base(query)
    if len(q) < 2:
        return []

    name_q, tel_q, plate_q = norm_name(q), norm_phone(q), norm_plate(q)
    # Brand needs its own normalizer (โตโยต้า -> toyota), or a Thai-language query finds nothing
    brand_q = norm_brand(q)
    status_clause = "" if include_pending else "AND review_status = 'approved'"

    # A low similarity threshold, because we rerank afterwards — this stage only needs a wide candidate net
    sql = f"""
        SELECT *,
               similarity(name_norm, %(name)s)   AS sim_name,
               similarity(plate_norm, %(plate)s) AS sim_plate
        FROM {DB_SCHEMA}.slips
        WHERE TRUE {status_clause}
          AND (
                name_norm %% %(name)s
             OR plate_norm %% %(plate)s
             OR brand_norm %% %(brand)s
             OR (%(tel)s <> '' AND tel_digits LIKE %(tel_like)s)
             OR (%(tel)s <> '' AND length(tel_digits) > 0
                 AND levenshtein(tel_digits, %(tel)s) <= 2)
             OR (%(plate)s <> '' AND length(plate_norm) > 0
                 AND levenshtein(plate_norm, %(plate)s) <= 2)
          )
        LIMIT {CANDIDATE_LIMIT}
    """
    rows = conn.execute(
        sql,
        {"name": name_q, "plate": plate_q, "tel": tel_q, "brand": brand_q,
         "tel_like": f"%{tel_q}%"},
    ).fetchall()

    kind = classify(q)
    scored = []
    for r in rows:
        score, why = _score(r, kind, name_q, tel_q, plate_q, brand_q)
        scored.append({**r, "score": score, "match_on": why})
    # Three sort layers: score first, then still-parked slips, then the most recent round.
    # The same car parked over several rounds scores identically on every slip (one set of
    # plate, name and phone). Without the second layer, the order of a parked slip against
    # an already-returned one depends on how Postgres happens to return rows, so exit staff
    # could open an already-closed round first.
    scored.sort(key=lambda r: r["created_at"], reverse=True)
    scored.sort(key=lambda r: (-r["score"], r["car_status"] != "stored"))
    return scored[:limit]


def _score(row: dict, kind: str, name_q: str, tel_q: str, plate_q: str, brand_q: str = "") -> tuple[float, str]:
    """Score 0-100 = similarity of the *best matching field*, not an average across fields.

    Averaging every field would score an exact phone-number match at only ~70%, which
    reads as a much weaker match than it is. So this takes the maximum, then applies a
    small weight based on what the user probably searched by, so the field matching the
    query's intent beats a field that merely happens to look similar.
    """
    tel = row.get("tel_digits") or ""
    plate = row.get("plate_norm") or ""
    name = row.get("name_norm") or ""
    brand = row.get("brand_norm") or ""

    s_tel = 100.0 if (tel_q and tel and tel_q in tel) else (fuzz.ratio(tel_q, tel) if tel_q and tel else 0)
    s_plate = fuzz.ratio(plate_q, plate) if plate_q and plate else 0
    # token_set_ratio: still matches with given and family name swapped. partial_ratio: still matches a half-typed name.
    s_name = max(fuzz.token_set_ratio(name_q, name), fuzz.partial_ratio(name_q, name)) if name_q and name else 0
    s_brand = fuzz.partial_ratio(brand_q, brand) if brand_q and brand else 0

    # Weights: the field matching the query type gets full weight, the others are discounted slightly
    prefer = {
        "tel": {"เบอร์โทร": 1.0, "ทะเบียน": 0.85, "ชื่อ": 0.80, "ยี่ห้อ": 0.60},
        "plate": {"เบอร์โทร": 0.85, "ทะเบียน": 1.0, "ชื่อ": 0.80, "ยี่ห้อ": 0.60},
        "name": {"เบอร์โทร": 0.80, "ทะเบียน": 0.85, "ชื่อ": 1.0, "ยี่ห้อ": 0.70},
    }[kind]

    parts = {"เบอร์โทร": s_tel, "ทะเบียน": s_plate, "ชื่อ": s_name, "ยี่ห้อ": s_brand}
    why, score = max(parts.items(), key=lambda kv: kv[1] * prefer[kv[0]])
    return round(score * prefer[why], 1), why
