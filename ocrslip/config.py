"""ค่า config ทั้งหมดอ่านจาก .env"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# override=True: ถ้า shell มี OPENROUTER_API_KEY ตัวเก่าค้างอยู่ ให้ .env ของโปรเจกต์ชนะเสมอ
load_dotenv(ROOT / ".env", override=True)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_SCHEMA = os.getenv("DB_SCHEMA", "ocr_dhammakaya")

# model ที่ใช้ตอน production — เลือกจาก bench/report.md (ชนะที่ 78% $1.55/1000 ใบ)
OCR_MODEL = os.getenv("OCR_MODEL", "google/gemini-3-flash-preview")
# เกณฑ์ confidence ที่ต่ำกว่านี้จะถูกส่งเข้าคิวตรวจสอบ
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))
# อายุการจองใบในคิวตรวจ — คนตรวจถือใบไว้ได้นานเท่านี้ก่อนใบหลุดกลับเข้าคิว
# ตั้งไว้ยาวไว้ก่อนโดยตั้งใจ: ใบค้างสิบนาทีไม่มีผลอะไรเมื่อคิวมีเป็นพันใบ
# แต่ถ้าสั้นไปคนตรวจที่ติดใบยาก ๆ จะโดนแย่งใบกลางคัน ซึ่งคือปัญหาเดิมที่กำลังแก้
REVIEW_CLAIM_MINUTES = int(os.getenv("REVIEW_CLAIM_MINUTES", "10"))
# ใช้แปลงค่าใช้จ่าย AI (OpenRouter คิดเป็น USD) ให้แสดงผลเป็นบาท
USD_THB = float(os.getenv("USD_THB", "33"))

# กุญแจสำหรับเซ็น session cookie — ถ้าไม่ตั้ง จะสุ่มใหม่ทุกครั้งที่รีสตาร์ต
# (ปลอดภัย แต่ผู้ใช้ทุกคนจะหลุดล็อกอิน) บน production ต้องตั้งค่านี้เสมอ
SECRET_KEY = os.getenv("SECRET_KEY", "")
# ตั้ง true เมื่อรันหลัง HTTPS (Render เป็น HTTPS อยู่แล้ว) เพื่อบังคับ Secure cookie
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
