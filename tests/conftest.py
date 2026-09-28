"""fixture ที่ใช้ร่วมกันของเทสต์ที่ต้องใช้ Postgres จริง

เทสต์ส่วนใหญ่ในโปรเจกต์นี้ไม่ต้องใช้ฐานข้อมูล แต่เรื่องคิวตรวจ (การจองใบ + กันเขียนทับ)
ทดสอบด้วย mock ไม่ได้ เพราะของที่ทดสอบคือ rowcount ของ UPDATE ที่มีเงื่อนไข
และ FOR UPDATE SKIP LOCKED ซึ่งเป็นพฤติกรรมของ Postgres ล้วน ๆ

ตั้ง OCRSLIP_TEST_DATABASE_URL ชี้ฐานข้อมูลที่ทิ้งได้ ไม่งั้นเทสต์กลุ่มนี้ถูก skip
"""

import os

import pytest

TEST_DB = os.getenv("OCRSLIP_TEST_DATABASE_URL", "")
TEST_SCHEMA = "ocr_test_queue"
TEST_PW = "pw-for-test"

needs_db = pytest.mark.skipif(
    not TEST_DB, reason="ต้องตั้ง OCRSLIP_TEST_DATABASE_URL ชี้ Postgres ที่ทิ้งข้อมูลได้"
)


import contextlib


@contextlib.contextmanager
def _pointed_at_test_db():
    """ชี้ปลายทางฐานข้อมูลของ ocrslip ไปที่ฐานข้อมูลทดสอบ แล้วคืนค่าเดิมเสมอ

    config.py อ่าน .env (ซึ่งชี้ production) ตั้งแต่ตอน import และโมดูลอื่นทำ
    `from .config import DATABASE_URL, DB_SCHEMA` เก็บไว้เป็นชื่อของตัวเอง
    การตั้ง env var เฉย ๆ จึงไม่มีผล ต้องเขียนทับที่ตัวโมดูลตรง ๆ

    ต้องคืนค่าเดิมทุกครั้งที่จบเทสต์ ไม่ใช่ตอนจบ session — เทสต์ไฟล์อื่น
    (test_web_headers, test_review_queue) อ่านฐานข้อมูลตาม DATABASE_URL จริง
    ถ้าปล่อยให้ค้างชี้ที่ฐานทดสอบ มันจะไปเจอข้อมูลสังเคราะห์ที่ไม่มีรูปแล้วพัง
    """
    from ocrslip import config, db, dedup, recheck, reprocess, search
    from ocrslip.web import pipeline

    targets = (config, db, dedup, search, recheck, reprocess, pipeline)
    saved = [(m, getattr(m, "DATABASE_URL", None), getattr(m, "DB_SCHEMA", None))
             for m in targets]
    for m in targets:
        if hasattr(m, "DATABASE_URL"):
            m.DATABASE_URL = TEST_DB
        if hasattr(m, "DB_SCHEMA"):
            m.DB_SCHEMA = TEST_SCHEMA
    # ด่านสุดท้าย: ถ้าหลุดไปชี้ที่อื่นเมื่อไหร่ ให้ตายตรงนี้ ก่อนจะมีคำสั่งเขียนสักตัว
    assert db.DATABASE_URL == TEST_DB, "db ไม่ได้ชี้ฐานข้อมูลทดสอบ"
    assert db.DB_SCHEMA == TEST_SCHEMA, "db ไม่ได้ใช้ schema ทดสอบ"
    try:
        yield
    finally:
        for m, url, schema in saved:
            if url is not None:
                m.DATABASE_URL = url
            if schema is not None:
                m.DB_SCHEMA = schema


@pytest.fixture(scope="session")
def _schema_ready():
    """สร้าง schema ทดสอบครั้งเดียวต่อ session (init_schema ยิงหลายสิบคำสั่ง)"""
    if not TEST_DB:
        pytest.skip("ไม่ได้ตั้ง OCRSLIP_TEST_DATABASE_URL")
    from ocrslip import auth, db

    with _pointed_at_test_db():
        db.init_schema()

    h = auth.hash_password(TEST_PW)
    for prefix, name in (("ADMIN", "admin"), ("USER", "staff"), ("APPROVE", "approve")):
        os.environ[f"{prefix}_USERNAME"] = name
        os.environ[f"{prefix}_PASSWORD_HASH"] = h


@pytest.fixture
def pgenv(_schema_ready):
    """ชี้ ocrslip ไปฐานข้อมูลทดสอบเฉพาะระหว่างเทสต์นี้เท่านั้น"""
    with _pointed_at_test_db():
        yield


@pytest.fixture
def make_slips(pgenv):
    """สร้างใบ pending N ใบ เรียงจากเก่าไปใหม่ (ใบแรก = หัวคิว) ล้างของเดิมก่อนเสมอ"""
    from ocrslip.db import connect

    def _make(n: int = 3) -> list[str]:
        with connect() as conn:
            conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
            ids = [
                conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slips
                        (name, tel, plate_raw, review_status, needs_review, created_at)
                        VALUES (%s, %s, %s, 'pending', true, now() - (%s || ' min')::interval)
                        RETURNING id::text""",
                    (f"ทดสอบ {i}", f"08100000{i % 100:02d}", f"1กก{i}00{i}", 10_000 - i),
                ).fetchone()["id"]
                for i in range(n)
            ]
            conn.commit()
        return ids

    return _make


@pytest.fixture
def worker(pgenv):
    """TestClient หนึ่งตัว = คนตรวจหนึ่งคน (คุกกี้ worker คนละใบ)

    สามคนที่นั่งตรวจใช้บัญชีเดียวกัน จะแยกกันได้ก็ด้วยคุกกี้ worker เท่านั้น
    TestClient แต่ละตัวมี cookie jar ของตัวเอง จึงจำลองได้ตรงกับของจริง
    """
    from fastapi.testclient import TestClient

    from ocrslip import auth
    from ocrslip.web.main import app

    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()

    def _worker(username: str = "staff"):
        c = TestClient(app)
        r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303, f"ล็อกอิน {username} ไม่ผ่าน"
        c.get("/review")  # ให้ middleware ออกคุกกี้ worker ให้ก่อน
        return c

    return _worker
