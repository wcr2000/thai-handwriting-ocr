"""Every config value is read from .env"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# override=True: if the shell still carries a stale OPENROUTER_API_KEY, the
# project's own .env must always win.
load_dotenv(ROOT / ".env", override=True)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_SCHEMA = os.getenv("DB_SCHEMA", "ocr_dhammakaya")

# Production model — picked from bench/report.md (winner at 78%, $1.55 per 1000 slips)
OCR_MODEL = os.getenv("OCR_MODEL", "google/gemini-3-flash-preview")
# Fields scoring below this confidence are routed into the review queue
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))
# How long a reviewer may hold a claimed slip before it falls back into the queue.
# Deliberately generous: a slip parked for ten minutes costs nothing when the queue
# holds thousands, but too short a lease means reviewers working a hard slip get it
# yanked mid-edit — which is the very problem this lease was added to fix.
REVIEW_CLAIM_MINUTES = int(os.getenv("REVIEW_CLAIM_MINUTES", "10"))
# Converts AI spend (OpenRouter bills in USD) for display in Thai baht
USD_THB = float(os.getenv("USD_THB", "33"))
# Timezone applied to every connection. The DB stores timestamptz (UTC), which is
# correct, but the Render server runs with TimeZone=UTC, so rendered times would sit
# 7 hours behind Thai local time. Setting it once per connection beats sprinkling +7
# through every template — and it makes the dashboard's "per day" buckets break at
# Thai midnight rather than UTC midnight, which locally is 7am.
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Bangkok")

# Key used to sign the session cookie. Unset means a fresh random key on every
# restart — safe, but it logs every user out. Production must always set this.
SECRET_KEY = os.getenv("SECRET_KEY", "")
# Set true when served behind HTTPS (Render already is) to force a Secure cookie
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")

# --- Self-service entry form filled in by the driver (/in) ---
# The code a staff member types to close out the form is their attestation that
# "I saw this car parked here". Verified server-side only — never move this check
# into JavaScript: in the page, the code would show up in view-source on every
# device that opens it, and guarding the code at all would become meaningless.
# No default on purpose: leaving this unset disables /in entirely, which beats
# opening it behind a code anyone could guess.
ENTRY_PASSWORD = os.getenv("ENTRY_PASSWORD", "")
# Comma-separated building/floor choices. A dropdown rather than a free-text box,
# because hand-typed values arrive as "อาคาร3ชั้น5" / "ตึก 3 ช.5" / "3-5", which
# cannot be sorted and cannot be aggregated on the dashboard.
ENTRY_BUILDINGS = tuple(
    s.strip() for s in os.getenv("ENTRY_BUILDINGS", "อาคาร 1,อาคาร 2,อาคาร 3,อาคาร 4,ลานจอดรอบนอก").split(",") if s.strip()
)
ENTRY_FLOORS = tuple(
    s.strip() for s in os.getenv("ENTRY_FLOORS", "ชั้น 1,ชั้น 2,ชั้น 3,ชั้น 4,ชั้น 5,ชั้น 6,ลานพื้นราบ").split(",") if s.strip()
)
