"""กันสองคนตรวจใบเดียวกันแล้วเขียนทับกัน (ทีมนั่งตรวจพร้อมกัน 3 คน)

ถึงจะมีการจองใบแล้ว (tests/test_review_claim.py) การชนก็ยังเกิดได้อยู่ดี — การจอง
หมดอายุได้ คนเปิดหน้าค้างไว้นาน ๆ ได้ และคลิกเข้าใบที่คนอื่นถืออยู่ได้ตรง ๆ
การจองแค่ทำให้ "ไม่ค่อยเจอกัน" ชุดนี้คุมอีกครึ่งคือ "เจอกันแล้วไม่พัง"

อาการที่หน้างานเจอคือช่อง "ผู้ตรวจ" เด้งเป็นชื่อคนอื่น (เทมเพลตให้ค่าใน DB ชนะ
ค่าที่จำไว้ในเครื่อง) แล้วถ้ากดอนุมัติต่อ งานของคนแรกก็ถูกทับเงียบ ๆ

ต้องใช้ Postgres จริง เพราะของที่ทดสอบคือ rowcount ของ UPDATE ที่มีเงื่อนไข
ดูวิธีรันที่ tests/conftest.py
"""

import threading

import pytest

from conftest import TEST_SCHEMA, needs_db

pytestmark = needs_db


def form(reviewer: str, **extra) -> dict:
    return {"name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
            "province": "กรุงเทพ", "brand": "รีโว่ แดง", "typecar": "เก๋ง",
            "location": "อาคาร 1", "date": "2026-09-26",
            "reviewed_by": reviewer, **extra}


def fetch(slip_id: str) -> dict:
    from ocrslip.db import connect, get_slip

    with connect() as conn:
        return get_slip(conn, slip_id)


def test_second_approve_does_not_overwrite_the_first(make_slips, worker):
    """คนที่สองกดอนุมัติใบที่เพื่อนตรวจไปแล้ว ต้องไม่ทับทั้งชื่อ เวลา และข้อมูล"""
    slips = make_slips(3)
    a, b = worker("admin"), worker("staff")
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


def test_blocked_approve_writes_no_audit_row(make_slips, worker):
    """ยิงซ้ำกี่ครั้ง slip_edits ต้องไม่งอก ไม่งั้นสถิติ "ใครแก้ field ไหนบ่อย" เพี้ยน"""
    from ocrslip.db import connect

    slips = make_slips(3)
    a, b = worker("admin"), worker("staff")
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


def test_reviewed_slip_shows_banner_instead_of_approve_button(make_slips, worker):
    """เดินตาม next_id เก่าไปเจอใบที่เพื่อนตรวจแล้ว ต้องรู้ตัว ไม่ใช่เห็นฟอร์มปกติ

    นี่คืออาการ "ชื่อผู้ตรวจเด้งไปเป็นของคนอื่น" ที่หน้างานเจอ
    """
    slips = make_slips(3)
    a, b = worker("admin"), worker("staff")
    x = slips[0]
    a.post(f"/review/{x}/approve", data=form("krit"), follow_redirects=False)

    page = b.get(f"/review/{x}").text
    assert "ใบนี้ตรวจไปแล้ว" in page
    assert 'class="actbar" hidden' in page, "แถบปุ่มอนุมัติต้องถูกซ่อน"
    # .actbar ตั้ง display:flex ไว้ ซึ่งชนะ [hidden] ของเบราว์เซอร์
    # ถ้าไม่มีกฎนี้ attribute hidden จะไม่มีผลจริง ปุ่มยังโผล่อยู่
    assert ".actbar[hidden] { display:none; }" in page, "ต้องมีกฎ CSS ที่ซ่อนได้จริง"
    # ไม่ลิงก์ไป id ตรง ๆ แล้ว — /review/next จองใบสด ๆ ให้ตอนกด จะได้ไม่ไปชนกับเพื่อนอีก
    assert "/review/next" in page, "ต้องมีทางไปใบถัดไปที่ยังไม่มีใครตรวจ"
    assert f"/review/{x}?edit=1" in page, "ต้องมีทางแก้ถ้าตั้งใจจริง"


def test_pending_slip_is_untouched(make_slips, worker):
    """ใบที่ยังไม่มีใครตรวจต้องทำงานเหมือนเดิมทุกอย่าง"""
    slips = make_slips(3)
    b = worker("staff")
    y, z = slips[1], slips[2]

    page = b.get(f"/review/{y}").text
    assert "ใบนี้ตรวจไปแล้ว" not in page
    assert 'class="actbar" hidden' not in page
    select = page.split('id="reviewed_by"')[1].split("</select>")[0]
    assert "selected" not in select, "ใบที่ยังไม่มีใครตรวจต้องไม่ preselect ชื่อใคร"

    r = b.post(f"/review/{y}/approve", data=form("somchai"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/next"
    assert fetch(y)["reviewed_by"] == "somchai"


def test_edit_query_param_allows_deliberate_fix(make_slips, worker):
    """เจ้าของใบตั้งใจกลับมาแก้เอง (?edit=1) ต้องยังแก้ได้ — ไม่ใช่ล็อกตายถาวร"""
    slips = make_slips(3)
    a = worker("admin")
    x = slips[0]
    a.post(f"/review/{x}/approve", data=form("krit"), follow_redirects=False)

    page = a.get(f"/review/{x}?edit=1").text
    assert "ใบนี้ตรวจไปแล้ว" not in page
    assert 'name="force" value="1"' in page

    r = a.post(f"/review/{x}/approve", data=form("krit", force="1", name="แก้ทีหลัง"),
               follow_redirects=False)
    assert r.status_code == 303
    assert fetch(x)["name"] == "แก้ทีหลัง"


def test_parallel_approve_has_exactly_one_winner(make_slips, worker):
    """ยิงพร้อมกันจริง ๆ สองเธรด ใบเดียวกัน ต้องผ่านคนเดียว"""
    from ocrslip.db import connect

    z = make_slips(3)[2]
    clients = {"a": worker("admin"), "b": worker("staff")}
    results: dict[str, int] = {}

    def hit(tag: str, who: str):
        results[tag] = clients[tag].post(f"/review/{z}/approve", data=form(who),
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
