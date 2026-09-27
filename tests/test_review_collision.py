"""กันสองคนตรวจใบเดียวกันแล้วเขียนทับกัน (ทีมนั่งตรวจพร้อมกัน 3 คน)

คิวยื่นใบหัวแถวใบเดียวกันให้ทุกคน และปุ่ม "ใบถัดไป" พาไปตาม id ที่คำนวณไว้ตอนเปิดหน้า
คนที่ตรวจช้ากว่าจึงไปโผล่ใบที่เพื่อนเพิ่งตรวจเสร็จได้เสมอ อาการที่หน้างานเจอคือ
ช่อง "ผู้ตรวจ" เด้งเป็นชื่อคนอื่น (เทมเพลตให้ค่าใน DB ชนะค่าที่จำไว้ในเครื่อง)
แล้วถ้ากดอนุมัติต่อ งานของคนแรกก็ถูกทับเงียบ ๆ

เทสต์ชุดนี้ต้องใช้ Postgres จริง เพราะของที่ทดสอบคือ rowcount ของ UPDATE ที่มีเงื่อนไข
ตั้ง OCRSLIP_TEST_DATABASE_URL ชี้ฐานข้อมูลที่ทิ้งได้ ไม่งั้นทั้งไฟล์ถูก skip:

    docker run -d --name ocrslip-test -e POSTGRES_PASSWORD=test \
        -e POSTGRES_DB=ocrslip -p 55439:5432 postgres:15
    OCRSLIP_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55439/ocrslip \
        .venv/bin/python -m pytest tests/test_review_collision.py -q
"""

import os
import threading

import pytest

TEST_DB = os.getenv("OCRSLIP_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="ต้องตั้ง OCRSLIP_TEST_DATABASE_URL ชี้ Postgres ที่ทิ้งข้อมูลได้"
)

TEST_SCHEMA = "ocr_test_collision"
TEST_PW = "pw-for-test"


@pytest.fixture(scope="module")
def env():
    """ชี้ปลายทางฐานข้อมูลของ ocrslip.db ไปที่ฐานข้อมูลทดสอบ

    config.py อ่าน .env (ซึ่งชี้ production) ตั้งแต่ตอน import และ db.py ทำ
    `from .config import DATABASE_URL, DB_SCHEMA` เก็บไว้เป็นชื่อของตัวเอง
    การตั้ง env var เฉย ๆ จึงไม่มีผล ถ้าโมดูลอื่นในชุดเทสต์ import ไปก่อนแล้ว
    ต้องเขียนทับที่ตัวโมดูลตรง ๆ ไม่งั้นเทสต์ชุดนี้จะยิงใส่ฐานข้อมูลจริง
    """
    from ocrslip import auth, config, db, search

    targets = (config, db, search)
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

    h = auth.hash_password(TEST_PW)
    for prefix, name in (("ADMIN", "admin"), ("USER", "staff"), ("APPROVE", "approve")):
        os.environ[f"{prefix}_USERNAME"] = name
        os.environ[f"{prefix}_PASSWORD_HASH"] = h

    db.init_schema()
    yield
    for m, url, schema in saved:
        if url is not None:
            m.DATABASE_URL = url
        if schema is not None:
            m.DB_SCHEMA = schema


@pytest.fixture
def slips(env):
    """ใบ pending 3 ใบ เรียงจากเก่าไปใหม่ (ใบแรก = หัวคิว)"""
    from ocrslip.db import connect

    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        ids = [
            conn.execute(
                f"""INSERT INTO {TEST_SCHEMA}.slips
                    (name, tel, plate_raw, review_status, needs_review, created_at)
                    VALUES (%s, %s, %s, 'pending', true, now() - (%s || ' min')::interval)
                    RETURNING id::text""",
                (f"ทดสอบ {i}", f"08100000{i:02d}", f"1กก{i}00{i}", 60 - i),
            ).fetchone()["id"]
            for i in range(3)
        ]
        conn.commit()
    return ids


@pytest.fixture
def login(env):
    from fastapi.testclient import TestClient

    from ocrslip import auth
    from ocrslip.web.main import app

    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()

    def _login(username: str):
        c = TestClient(app)
        r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303, f"ล็อกอิน {username} ไม่ผ่าน"
        return c

    return _login


def form(reviewer: str, next_id: str = "", **extra) -> dict:
    return {"name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
            "province": "กรุงเทพ", "brand": "รีโว่ แดง", "typecar": "เก๋ง",
            "location": "อาคาร 1", "date": "2026-09-26",
            "reviewed_by": reviewer, "next_id": next_id, **extra}


def fetch(slip_id: str) -> dict:
    from ocrslip.db import connect, get_slip

    with connect() as conn:
        return get_slip(conn, slip_id)


def test_second_approve_does_not_overwrite_the_first(slips, login):
    """คนที่สองกดอนุมัติใบที่เพื่อนตรวจไปแล้ว ต้องไม่ทับทั้งชื่อ เวลา และข้อมูล"""
    a, b = login("admin"), login("staff")
    x = slips[0]

    assert a.post(f"/review/{x}/approve", data=form("krit"),
                  follow_redirects=False).status_code == 303
    first = fetch(x)
    assert first["reviewed_by"] == "krit"

    r = b.post(f"/review/{x}/approve", data=form("somchai", name="ชื่อที่จะทับ"),
               follow_redirects=False)
    after = fetch(x)

    assert r.status_code == 200, "ต้องไม่ redirect ไปใบถัดไปเหมือนอนุมัติสำเร็จ"
    assert after["reviewed_by"] == "krit"
    assert after["reviewed_at"] == first["reviewed_at"]
    assert after["name"] == first["name"]
    assert "ใบนี้ตรวจไปแล้ว" in r.text and "krit" in r.text


def test_blocked_approve_writes_no_audit_row(slips, login):
    """ยิงซ้ำกี่ครั้ง slip_edits ต้องไม่งอก ไม่งั้นสถิติ "ใครแก้ field ไหนบ่อย" เพี้ยน"""
    from ocrslip.db import connect

    a, b = login("admin"), login("staff")
    x = slips[0]
    a.post(f"/review/{x}/approve", data=form("krit"), follow_redirects=False)

    def edit_count() -> int:
        with connect() as conn:
            return conn.execute(
                f"SELECT count(*) c FROM {TEST_SCHEMA}.slip_edits WHERE slip_id = %s", (x,)
            ).fetchone()["c"]

    before = edit_count()
    for _ in range(3):
        b.post(f"/review/{x}/approve", data=form("somchai"), follow_redirects=False)
    assert edit_count() == before


def test_reviewed_slip_shows_banner_instead_of_approve_button(slips, login):
    """เดินตาม next_id เก่าไปเจอใบที่เพื่อนตรวจแล้ว ต้องรู้ตัว ไม่ใช่เห็นฟอร์มปกติ

    นี่คืออาการ "ชื่อผู้ตรวจเด้งไปเป็นของคนอื่น" ที่หน้างานเจอ
    """
    a, b = login("admin"), login("staff")
    x = slips[0]
    a.post(f"/review/{x}/approve", data=form("krit"), follow_redirects=False)

    page = b.get(f"/review/{x}").text
    assert "ใบนี้ตรวจไปแล้ว" in page
    assert 'class="actbar" hidden' in page, "แถบปุ่มอนุมัติต้องถูกซ่อน"
    # .actbar ตั้ง display:flex ไว้ ซึ่งชนะ [hidden] ของเบราว์เซอร์
    # ถ้าไม่มีกฎนี้ attribute hidden จะไม่มีผลจริง ปุ่มยังโผล่อยู่
    assert ".actbar[hidden] { display:none; }" in page, "ต้องมีกฎ CSS ที่ซ่อนได้จริง"
    assert f"/review/{slips[1]}" in page, "ต้องมีทางไปใบถัดไปที่ยังไม่มีใครตรวจ"
    assert f"/review/{x}?edit=1" in page, "ต้องมีทางแก้ถ้าตั้งใจจริง"


def test_pending_slip_is_untouched(slips, login):
    """ใบที่ยังไม่มีใครตรวจต้องทำงานเหมือนเดิมทุกอย่าง"""
    b = login("staff")
    y, z = slips[1], slips[2]

    page = b.get(f"/review/{y}").text
    assert "ใบนี้ตรวจไปแล้ว" not in page
    assert 'class="actbar" hidden' not in page
    select = page.split('id="reviewed_by"')[1].split("</select>")[0]
    assert "selected" not in select, "ใบที่ยังไม่มีใครตรวจต้องไม่ preselect ชื่อใคร"

    r = b.post(f"/review/{y}/approve", data=form("somchai", next_id=z), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/review/{z}"
    assert fetch(y)["reviewed_by"] == "somchai"


def test_edit_query_param_allows_deliberate_fix(slips, login):
    """เจ้าของใบตั้งใจกลับมาแก้เอง (?edit=1) ต้องยังแก้ได้ — ไม่ใช่ล็อกตายถาวร"""
    a = login("admin")
    x = slips[0]
    a.post(f"/review/{x}/approve", data=form("krit"), follow_redirects=False)

    page = a.get(f"/review/{x}?edit=1").text
    assert "ใบนี้ตรวจไปแล้ว" not in page
    assert 'name="force" value="1"' in page

    r = a.post(f"/review/{x}/approve", data=form("krit", force="1", name="แก้ทีหลัง"),
               follow_redirects=False)
    assert r.status_code == 303
    assert fetch(x)["name"] == "แก้ทีหลัง"


def test_parallel_approve_has_exactly_one_winner(slips, login):
    """ยิงพร้อมกันจริง ๆ สองเธรด ใบเดียวกัน ต้องผ่านคนเดียว"""
    from ocrslip.db import connect

    z = slips[2]
    results: dict[str, int] = {}

    def hit(tag: str, who: str):
        client = login("admin" if tag == "a" else "staff")
        results[tag] = client.post(f"/review/{z}/approve", data=form(who),
                                   follow_redirects=False).status_code

    threads = [threading.Thread(target=hit, args=("a", "krit")),
               threading.Thread(target=hit, args=("b", "somchai"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [200, 303], f"ต้องผ่านคนเดียว ได้ {results}"
    assert fetch(z)["review_status"] == "approved"
    with connect() as conn:
        editors = conn.execute(
            f"SELECT count(DISTINCT edited_by) c FROM {TEST_SCHEMA}.slip_edits WHERE slip_id = %s",
            (z,),
        ).fetchone()["c"]
    assert editors <= 1, "audit log ต้องมีคนแก้คนเดียว"
