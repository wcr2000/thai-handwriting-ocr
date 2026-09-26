"""ค้นหาแบบ fuzzy: ชื่อสะกดผิด เบอร์เพี้ยน ทะเบียนอ่านผิด ก็ยังเจอใบที่ใกล้ที่สุด

ทำสองชั้น: ดึง candidate จาก Postgres ด้วย trigram/levenshtein (เร็ว ใช้ index)
แล้ว rerank ใน Python ด้วย rapidfuzz เพื่อให้จัดอันดับดีกว่า similarity ดิบ ๆ
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
    """เดาว่าผู้ใช้พิมพ์อะไรมา: เบอร์โทร / ทะเบียน / ชื่อ"""
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
    # ยี่ห้อต้อง normalize คนละแบบ (โตโยต้า -> toyota) ไม่งั้นค้นภาษาไทยจะไม่เจอ
    brand_q = norm_brand(q)
    status_clause = "" if include_pending else "AND review_status = 'approved'"

    # similarity threshold ต่ำ ๆ เพราะเราจะไป rerank เองอีกที — ตรงนี้เอาแค่ candidate กว้าง ๆ
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
    scored.sort(key=lambda r: -r["score"])
    return scored[:limit]


def _score(row: dict, kind: str, name_q: str, tel_q: str, plate_q: str, brand_q: str = "") -> tuple[float, str]:
    """คะแนน 0-100 = ความเหมือนของ "ช่องที่แมตช์ดีที่สุด" ไม่ใช่ค่าเฉลี่ยรวมทุกช่อง

    ถัวเฉลี่ยทุกช่องจะทำให้เบอร์ที่ตรงเป๊ะได้แค่ ~70% ซึ่งอ่านแล้วเข้าใจผิด
    ที่นี่จึงใช้ค่าสูงสุด แล้วคูณตัวถ่วงเล็กน้อยตามว่าผู้ใช้น่าจะค้นด้วยอะไร
    เพื่อให้ช่องที่ตรงกับเจตนาของ query ชนะช่องที่บังเอิญคล้าย
    """
    tel = row.get("tel_digits") or ""
    plate = row.get("plate_norm") or ""
    name = row.get("name_norm") or ""
    brand = row.get("brand_norm") or ""

    s_tel = 100.0 if (tel_q and tel and tel_q in tel) else (fuzz.ratio(tel_q, tel) if tel_q and tel else 0)
    s_plate = fuzz.ratio(plate_q, plate) if plate_q and plate else 0
    # token_set_ratio: สลับชื่อ-นามสกุลก็ยังแมตช์ / partial_ratio: พิมพ์ชื่อไม่จบก็ยังแมตช์
    s_name = max(fuzz.token_set_ratio(name_q, name), fuzz.partial_ratio(name_q, name)) if name_q and name else 0
    s_brand = fuzz.partial_ratio(brand_q, brand) if brand_q and brand else 0

    # ตัวถ่วง: ช่องที่ตรงกับชนิดของ query ได้น้ำหนักเต็ม ช่องอื่นถูกลดทอนเล็กน้อย
    prefer = {
        "tel": {"เบอร์โทร": 1.0, "ทะเบียน": 0.85, "ชื่อ": 0.80, "ยี่ห้อ": 0.60},
        "plate": {"เบอร์โทร": 0.85, "ทะเบียน": 1.0, "ชื่อ": 0.80, "ยี่ห้อ": 0.60},
        "name": {"เบอร์โทร": 0.80, "ทะเบียน": 0.85, "ชื่อ": 1.0, "ยี่ห้อ": 0.70},
    }[kind]

    parts = {"เบอร์โทร": s_tel, "ทะเบียน": s_plate, "ชื่อ": s_name, "ยี่ห้อ": s_brand}
    why, score = max(parts.items(), key=lambda kv: kv[1] * prefer[kv[0]])
    return round(score * prefer[why], 1), why
