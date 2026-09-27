"""การจองใบในคิว — สามคนที่นั่งตรวจพร้อมกันต้องไม่ได้ใบเดียวกัน

เดิม next_in_queue() ยื่นใบหัวแถวใบเดียวกันให้ทุกคน และปุ่ม "ใบถัดไป" เดินตาม id
ที่คำนวณไว้ตั้งแต่ตอน render ทุกคนจึงไล่คิวเส้นทางเดียวกันเป๊ะ

ต้องใช้ Postgres จริง — ของที่ทดสอบคือ FOR UPDATE SKIP LOCKED กับเงื่อนไขอายุการจอง
ซึ่ง mock ไม่ได้ ดูวิธีรันที่ tests/conftest.py
"""

import threading

import pytest

from conftest import TEST_SCHEMA, needs_db

pytestmark = needs_db


def claimed_id(client) -> str | None:
    """กด "ใบถัดไป" แล้วคืน id ที่จองได้ (None = คิวหมด)"""
    r = client.get("/review/next", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    loc = r.headers["location"]
    return None if loc == "/review" else loc.rsplit("/", 1)[-1]


def claim_row(slip_id: str) -> dict:
    from ocrslip.db import connect

    with connect() as conn:
        return conn.execute(
            f"SELECT claimed_by, claimed_name, claimed_at, review_status"
            f" FROM {TEST_SCHEMA}.slips WHERE id = %s", (slip_id,)
        ).fetchone()


def test_two_reviewers_get_different_slips(make_slips, worker):
    """หัวใจของ PR นี้ — สองคนกดขอใบ ต้องได้คนละใบ"""
    make_slips(3)
    a, b = worker("admin"), worker("staff")
    assert claimed_id(a) != claimed_id(b)


APPROVE_FORM = {"name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
                "province": "กรุงเทพ", "brand": "รีโว่", "typecar": "เก๋ง",
                "location": "อาคาร 1", "date": "2026-09-26", "reviewed_by": "krit"}


def approve(client, slip_id: str):
    return client.post(f"/review/{slip_id}/approve", data=APPROVE_FORM,
                       follow_redirects=False)


def test_two_reviewers_drain_the_queue_without_doing_the_same_slip_twice(make_slips, worker):
    """จำลองการทำงานจริง: จอง → อนุมัติ → ขอใบถัดไป สลับกันสองคนจนคิวหมด

    ถ้าไม่อนุมัติแล้ววนขอใบถัดไปเฉย ๆ คิวจะไม่มีวันหมด เพราะการขอใบใหม่คืนใบเดิม
    เข้าคิวเสมอ (คนหนึ่งถือได้ทีละใบ) — ตัวที่ทำให้ใบออกจากกองคือการอนุมัติเท่านั้น
    """
    ids = make_slips(6)
    a, b = worker("admin"), worker("staff")
    done: list[str] = []
    for _ in range(20):  # กันลูปไม่รู้จบถ้าตรรกะพัง
        progressed = False
        for client in (a, b):
            got = claimed_id(client)
            if got is None:
                continue
            assert got not in done, f"ใบ {got} ถูกจ่ายซ้ำหลังตรวจไปแล้ว"
            approve(client, got)
            done.append(got)
            progressed = True
        if not progressed:
            break
    assert sorted(done) == sorted(ids), "ต้องตรวจครบทุกใบ ไม่ซ้ำ ไม่ขาด"


def test_a_reviewer_never_holds_two_slips_at_once(make_slips, worker):
    """คนหนึ่งถือได้ทีละใบ ไม่งั้นคนที่คลิกไปมาจะจองค้างไว้เต็มคิว แล้วคนอื่นต้องรอ
    จนหมดอายุทั้งที่ไม่มีใครนั่งตรวจใบพวกนั้นอยู่จริง
    """
    from ocrslip.db import connect

    ids = make_slips(3)
    a = worker("admin")
    a.get(f"/review/{ids[2]}")           # เปิดใบท้ายคิวตรง ๆ = จองใบนั้น
    assert claim_row(ids[2])["claimed_by"] is not None

    nxt = claimed_id(a)                  # แล้วขอใบถัดไปตามคิวปกติ
    assert nxt == ids[0], "ต้องได้ใบหัวคิว"
    assert claim_row(ids[2])["claimed_by"] is None, "ใบเดิมต้องถูกปล่อยคืนคิว"

    with connect() as conn:
        held = conn.execute(
            f"SELECT count(*) c FROM {TEST_SCHEMA}.slips WHERE claimed_by IS NOT NULL"
        ).fetchone()["c"]
    assert held == 1, f"คนเดียวต้องถือใบเดียว แต่มีใบถูกถืออยู่ {held} ใบ"


def test_asking_again_without_approving_returns_the_same_slip(make_slips, worker):
    """ขอใบถัดไปโดยยังไม่อนุมัติ ต้องได้ใบเดิม ไม่ใช่ข้ามไปเรื่อย ๆ

    เพราะปล่อยใบเดิมคืนคิวแล้วหยิบหัวคิวใหม่ ซึ่งก็คือใบเดิม — ที่สำคัญคือมันต้อง
    ไม่ทำให้ใบถูกข้ามหายไปจากคิวโดยไม่มีใครตรวจ
    """
    ids = make_slips(3)
    a = worker("admin")
    assert claimed_id(a) == ids[0]
    assert claimed_id(a) == ids[0]


def test_expired_claim_goes_back_into_the_queue(make_slips, worker):
    """คนถือใบแล้วปิดแท็บ ใบต้องกลับเข้าคิวเอง ไม่ใช่ค้างถาวรรอ admin มาปลด"""
    from ocrslip.db import connect

    ids = make_slips(1)
    a, b = worker("admin"), worker("staff")
    assert claimed_id(a) == ids[0]
    assert claimed_id(b) is None, "ระหว่างที่ยังไม่หมดอายุ คนอื่นต้องไม่ได้ใบนี้"

    with connect() as conn:  # ย้อนเวลาการจองให้เลยอายุไป
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips"
                     f" SET claimed_at = now() - interval '999 min' WHERE id = %s", (ids[0],))
        conn.commit()
    assert claimed_id(b) == ids[0]


def test_opening_a_slip_claims_it(make_slips, worker):
    """คลิกจากรายการคิวก็ต้องจอง ไม่งั้นสองคนที่คลิกแถวเดียวกันยังชนกันอยู่"""
    ids = make_slips(2)
    a, b = worker("admin"), worker("staff")
    a.get(f"/review/{ids[1]}")
    assert claim_row(ids[1])["claimed_by"] is not None
    assert claimed_id(b) == ids[0], "คนอื่นต้องได้ใบที่เหลือ ไม่ใช่ใบที่ถูกเปิดอยู่"


def test_opening_someone_elses_slip_warns_but_does_not_block(make_slips, worker):
    """การจองเป็นคำแนะนำ ไม่ใช่การล็อก — ยังเข้าดูได้ แต่ต้องรู้ว่ามีคนถืออยู่"""
    ids = make_slips(2)
    a, b = worker("admin"), worker("staff")
    held = claimed_id(a)

    page = b.get(f"/review/{held}")
    assert page.status_code == 200
    assert "กำลังตรวจใบนี้อยู่" in page.text
    assert "/review/next" in page.text, "ต้องมีทางขอใบอื่นที่ยังว่าง"
    assert claim_row(held)["claimed_by"] is not None


def test_approve_hands_out_a_freshly_claimed_slip(make_slips, worker):
    """อนุมัติแล้วต้องได้ใบใหม่ที่จองสด ๆ ไม่ใช่ id ที่ฝังไว้ตอนเปิดหน้า"""
    ids = make_slips(3)
    a, b = worker("admin"), worker("staff")
    mine = claimed_id(a)
    theirs = claimed_id(b)

    r = a.post(f"/review/{mine}/approve", follow_redirects=False, data={
        "name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
        "province": "กรุงเทพ", "brand": "รีโว่", "typecar": "เก๋ง",
        "location": "อาคาร 1", "date": "2026-09-26", "reviewed_by": "krit"})
    assert r.headers["location"] == "/review/next"

    nxt = claimed_id(a)
    assert nxt not in (mine, theirs), "ต้องไม่ได้ใบที่ตรวจไปแล้ว และไม่ใช่ใบที่เพื่อนถืออยู่"
    assert claim_row(mine)["review_status"] == "approved"
    assert claim_row(mine)["claimed_by"] is None, "ใบที่ออกจากคิวแล้วต้องไม่มีใครถือ"


@pytest.mark.parametrize("n_workers", [6])
def test_parallel_claims_never_hand_out_the_same_slip(make_slips, worker, n_workers):
    """ยิงพร้อมกันจริง ๆ — นี่คือเคสที่ FOR UPDATE SKIP LOCKED มีไว้กัน

    เงื่อนไข claimed_at กันใบที่จอง+commit ไปแล้ว แต่ไม่เห็น transaction ที่ยังค้างอยู่
    ถ้าขาด SKIP LOCKED สองคนที่ยิงห่างกันเป็นมิลลิวินาทีจะได้ใบเดียวกัน
    """
    make_slips(n_workers)
    clients = [worker("admin" if i % 2 else "staff") for i in range(n_workers)]
    got: list[str | None] = [None] * n_workers

    def hit(i: int):
        got[i] = claimed_id(clients[i])

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    handed = [g for g in got if g]
    assert len(handed) == len(set(handed)), f"มีใบถูกจ่ายซ้ำ: {got}"
    assert len(handed) == n_workers, f"ควรจ่ายได้ครบทุกคน ได้ {got}"


def test_recheck_clears_stale_claims(make_slips, worker):
    """ใบที่ถูกดึงกลับเข้าคิวต้องไม่พกการจองเก่ามาด้วย ไม่งั้นไม่มีใครได้ใบนั้นไปสิบนาที"""
    from ocrslip.db import connect
    from ocrslip.recheck import send_back

    ids = make_slips(1)
    a = worker("admin")
    assert claimed_id(a) == ids[0]

    with connect() as conn:  # จำลองใบที่ถูกอนุมัติไปแล้วทั้งที่ยังมีการจองค้าง
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET review_status='approved',"
                     f" needs_review=false WHERE id=%s", (ids[0],))
        send_back(conn, ids[0])
        conn.commit()

    row = claim_row(ids[0])
    assert row["review_status"] == "pending"
    assert row["claimed_by"] is None and row["claimed_at"] is None
