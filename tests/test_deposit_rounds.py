"""รถคันเดิมที่เอามาฝากหลายรอบ ต้องดูออกว่าแถวไหน/ใบไหนคือรอบที่เท่าไหร่

ที่มา: มีคนเอารถมาฝาก รับกลับ แล้วเอามาฝากใหม่ เห็นมาแล้ว 2-3 รอบต่อคัน
ข้อมูลถูกอยู่แล้ว (1 ใบ = 1 รอบ) แต่หน้าค้นหาคืนมาเป็นแถวคล้าย ๆ กันเรียงตามคะแนน
เจ้าหน้าที่ขาออกจึงต้องไล่อ่านวันที่เองว่าใบไหนคือรอบปัจจุบัน ปิดผิดใบเมื่อไหร่
จะเหลือใบค้างที่ไม่มีใครมารับตลอดไป

ต้องใช้ Postgres จริง — ของที่ทดสอบคือ window function กับลำดับแถวที่ SQL คืนมา
ดูวิธีรันที่ tests/conftest.py
"""

import pytest

from conftest import TEST_SCHEMA, needs_db

pytestmark = needs_db

PLATE, TEL = "กก1234", "0810000044"


@pytest.fixture
def rounds(pgenv):
    """สร้างใบของทะเบียนเดียวกัน N รอบ (วันละรอบ เก่าไปใหม่) คืน id เรียงตามรอบ"""
    from ocrslip.db import connect
    from ocrslip.normalize import norm_phone, norm_plate

    def _make(n: int = 3, *, plate: str = PLATE, returned_except_last: bool = True):
        with connect() as conn:
            ids = []
            for i in range(n):
                last = i == n - 1
                ids.append(conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slips
                        (name, tel, tel_digits, plate_raw, plate_norm, deposit_date, location,
                         review_status, needs_review, car_status, returned_at, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'approved', false, %s, %s,
                                now() - (%s || ' day')::interval)
                        RETURNING id::text""",
                    ("สมหมาย ทดสอบ", TEL, norm_phone(TEL), plate, norm_plate(plate),
                     f"2026-09-{10 + i:02d}", f"อาคาร {i + 1} ชั้น 2",
                     "stored" if (last or not returned_except_last) else "returned",
                     None if (last or not returned_except_last) else "2026-09-20",
                     n - i),
                ).fetchone()["id"])
            conn.commit()
        return ids

    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.commit()
    return _make


@pytest.fixture
def dup_day(rounds):
    """ใบของทะเบียนเดียวกัน N ใบใน "วันเดียวกัน" = ใบกระดาษใบเดียวที่ถูกอัปซ้ำ

    จงใจให้ที่จอดกับสถานะรถไม่เหมือนกัน — นั่นคือเหตุผลที่ของจริงหลุดตัวจับซ้ำ
    ตอนอนุมัติมาได้ (db.same_slip() ต้องการที่จอดตรงกันและยังไม่คืนรถทั้งคู่)
    """
    from ocrslip.db import connect
    from ocrslip.normalize import norm_phone, norm_plate

    assert rounds  # พึ่ง fixture เดิมเพื่อล้างตาราง ไม่ต้องมีสองที่ที่รู้วิธีล้าง

    def _make(n: int = 3, *, day: str | None = "2026-09-26"):
        with connect() as conn:
            ids = [conn.execute(
                f"""INSERT INTO {TEST_SCHEMA}.slips
                    (name, tel, tel_digits, plate_raw, plate_norm, deposit_date, location,
                     review_status, needs_review, car_status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'approved', false, %s,
                            now() + (%s || ' second')::interval)
                    RETURNING id::text""",
                ("สมศรี ทดสอบ", TEL, norm_phone(TEL), PLATE, norm_plate(PLATE), day,
                 ["", "ดำ", " ดำ "][i % 3], "returned" if i == 2 else "stored", i),
            ).fetchone()["id"] for i in range(n)]
            conn.commit()
        return ids

    return _make


def test_a_car_deposited_once_gets_no_round_label(rounds):
    """ใบเดี่ยว ๆ ไม่ต้องติดป้าย "รอบที่ 1 จาก 1" — มีแต่รกตา"""
    from ocrslip.db import connect, deposit_rounds

    only = rounds(1)
    with connect() as conn:
        assert deposit_rounds(conn, [_plate_norm()]) == {}
        assert only  # ใบมีอยู่จริง แค่ไม่ติดป้าย


def test_rounds_are_numbered_by_deposit_date_oldest_first(rounds):
    """รอบที่ 1 ต้องเป็นการฝากครั้งแรก ไม่ใช่ใบที่บันทึกเข้าระบบก่อน"""
    from ocrslip.db import connect, deposit_rounds

    first, second, third = rounds(3)
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm()])

    assert [got[i]["round"] for i in (first, second, third)] == [1, 2, 3]
    assert {got[i]["total"] for i in (first, second, third)} == {3}


def test_superseded_and_rejected_slips_are_not_rounds(rounds):
    """ใบซ้ำกับใบที่ตีกลับไม่ใช่การฝากจริงสักรอบ ต้องไม่ถูกนับรวมจนเลขรอบเพี้ยน"""
    from ocrslip.db import connect, deposit_rounds

    first, second, third = rounds(3)
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET review_status='rejected' WHERE id=%s",
                     (second,))
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET superseded_by=%s WHERE id=%s",
                     (first, third))
        conn.commit()
        got = deposit_rounds(conn, [_plate_norm()])

    assert got == {}, "เหลือรอบจริงใบเดียว จึงไม่ต้องติดป้ายเลย"


def test_rounds_of_other_plates_never_bleed_in(rounds):
    """รถคนละคันต้องนับรอบแยกกัน ไม่ใช่นับรวมเป็นกองเดียว"""
    from ocrslip.db import connect, deposit_rounds
    from ocrslip.normalize import norm_plate

    mine = rounds(2)
    other = rounds(2, plate="1กก1111")
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm(), norm_plate("1กก1111")])

    assert {got[i]["total"] for i in mine + other} == {2}
    assert [got[i]["round"] for i in other] == [1, 2]


def test_history_shows_every_round_with_its_slot_and_status(rounds):
    """หน้าใบต้องบอกได้ว่ารอบไหนจอดช่องไหน และรอบไหนยังไม่ได้รับรถกลับ"""
    from ocrslip.db import connect, deposit_history

    ids = rounds(3)
    with connect() as conn:
        hist = deposit_history(conn, _plate_norm())

    assert [h["id"] for h in hist] == ids
    assert [h["round"] for h in hist] == [1, 2, 3]
    assert [h["car_status"] for h in hist] == ["returned", "returned", "stored"]
    assert hist[2]["location"] == "อาคาร 3 ชั้น 2"


def test_history_of_an_unknown_plate_is_empty(pgenv):
    """ใบที่อ่านทะเบียนไม่ออก (plate_norm ว่าง) ต้องไม่ไปกวาดใบอื่นที่ก็ว่างเหมือนกัน"""
    from ocrslip.db import connect, deposit_history

    with connect() as conn:
        assert deposit_history(conn, "") == []


# ---------- หน้าเว็บ ----------

def test_search_puts_the_round_still_parked_first(rounds, worker):
    """ค้นทะเบียนแล้วแถวแรกต้องเป็นรอบที่รถยังจอดอยู่ ไม่ใช่รอบที่ปิดไปแล้ว

    ใบทุกรอบได้คะแนนเท่ากันหมด (ทะเบียน/ชื่อ/เบอร์ชุดเดียวกัน) ลำดับจึงขึ้นกับชั้นรอง
    ที่นี่จงใจให้ใบรอบที่ปิดไปแล้วเป็นใบที่บันทึกล่าสุด — ถ้าเรียงตามเวลาบันทึกอย่างเดียว
    ใบที่รับรถไปแล้วจะขึ้นก่อน ซึ่งคือใบที่เจ้าหน้าที่ขาออกต้องไม่เปิดเป็นใบแรก
    """
    from ocrslip.db import connect

    ids = rounds(3)
    with connect() as conn:  # ใบรอบแรก (รับรถไปแล้ว) ถูกบันทึกเข้าระบบทีหลังสุด
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET created_at = now() WHERE id=%s",
                     (ids[0],))
        conn.commit()

    page = worker("staff").get(f"/api/search?q={PLATE}").text

    # เรียงตามตำแหน่งที่ปรากฏในหน้า ไม่ใช่ลำดับที่สร้างใบ
    order = sorted((i for i in ids if f"/slips/{i}" in page), key=lambda i: page.index(i))
    assert order[0] == ids[-1], "ใบรอบที่รถยังจอดอยู่ต้องมาก่อนใบที่รับรถไปแล้ว"


def test_search_labels_which_round_each_row_is(rounds, worker):
    ids = rounds(3)
    page = worker("staff").get(f"/api/search?q={PLATE}").text

    assert "ฝากรอบที่ 3 จาก 3" in page and "ฝากรอบที่ 1 จาก 3" in page
    assert len(ids) == 3


def test_slip_page_lists_all_rounds_of_the_plate(rounds, worker):
    """จุดที่กดปล่อยรถต้องเห็นทุกรอบตรงนั้นเลย ไม่ต้องย้อนกลับไปหน้าค้นหา"""
    ids = rounds(3)
    page = worker("staff").get(f"/slips/{ids[1]}").text

    assert "ทะเบียนนี้ฝากมาแล้ว 3 รอบ" in page
    assert "← ใบนี้" in page
    assert f"/slips/{ids[2]}" in page, "ต้องกระโดดไปรอบอื่นได้"


def test_slip_page_of_a_single_round_has_no_history_card(rounds, worker):
    """ใบที่ฝากรอบเดียวไม่ต้องมีการ์ดประวัติที่มีแถวเดียวคือตัวมันเอง"""
    only = rounds(1)
    page = worker("staff").get(f"/slips/{only[0]}").text

    assert "ฝากมาแล้ว" not in page


# ---------- ใบเดียวกันที่ถูกอัปซ้ำ (วันเดียวกัน) ----------

def test_slips_of_the_same_day_are_one_round_not_many(dup_day):
    """ใบซ้ำสามใบของวันเดียวกันต้องไม่กลายเป็น "ฝากรอบที่ 1/2/3 จาก 3"

    ที่มา: ใบกระดาษใบเดียวถูกถ่ายมาสามรูป ทั้งสามหลุดตัวจับซ้ำตอนอนุมัติมาได้
    (ที่จอดพิมพ์ไม่เท่ากัน ใบหนึ่งว่าง อีกใบถูกปิดไปแล้ว) ป้ายบอกรอบจึงไปอ่านว่า
    รถคันนี้มาฝากสามหน ซึ่งไม่จริงและทำให้เจ้าหน้าที่ขาออกไล่ปิดใบผิด
    """
    from ocrslip.db import connect, deposit_rounds

    ids = dup_day(3)
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm()])

    assert [got[i]["round"] for i in ids] == [1, 1, 1]
    assert {got[i]["total"] for i in ids} == {1}, "วันเดียว = รอบเดียว"
    assert {got[i]["dup"] for i in ids} == {3}, "ต้องบอกได้ว่าวันนั้นมีสามใบ"


def test_search_calls_same_day_slips_duplicates_not_extra_rounds(dup_day, worker):
    ids = dup_day(3)
    page = worker("staff").get(f"/api/search?q={PLATE}").text

    assert "อาจเป็นใบซ้ำ · วันนี้มี 3 ใบ" in page
    assert "ฝากรอบที่" not in page, "รอบเดียว ไม่ต้องมีป้ายบอกรอบ"
    assert len(ids) == 3


def test_a_real_second_round_still_counts_even_with_a_duplicate(dup_day):
    """ฝากจริงสองรอบ + รอบแรกมีใบซ้ำ = ยังต้องเป็น "จาก 2 รอบ" ไม่ใช่ 3"""
    from ocrslip.db import connect, deposit_rounds

    twins = dup_day(2, day="2026-09-10")
    later = dup_day(1, day="2026-09-20")[0]
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm()])

    assert [got[i]["round"] for i in twins] == [1, 1]
    assert got[later]["round"] == 2
    assert {got[i]["total"] for i in twins + [later]} == {2}
    assert got[later]["dup"] == 1, "รอบที่สองมีใบเดียว ไม่ต้องฟ้องว่าซ้ำ"


def test_slips_without_a_deposit_date_are_not_duplicates_of_each_other(dup_day):
    """ใบที่อ่านวันที่ไม่ออกทุกใบมีวันเดียวกันคือ NULL — ต้องไม่ถูกเหมาว่าซ้ำกันหมด"""
    from ocrslip.db import connect, deposit_rounds

    ids = dup_day(2, day=None)
    with connect() as conn:
        got = deposit_rounds(conn, [_plate_norm()])

    assert {got[i]["dup"] for i in ids} == {1}
    assert {got[i]["total"] for i in ids} == {2}, "แยกไม่ได้ ก็ต้องนับเป็นคนละรอบไว้ก่อน"


def test_slip_page_marks_the_duplicate_rows(dup_day, worker):
    ids = dup_day(2)
    page = worker("staff").get(f"/slips/{ids[0]}").text

    assert "ทะเบียนนี้ฝากมาแล้ว 1 รอบ" in page and "(2 ใบ)" in page
    assert "ซ้ำ" in page
    assert f"/slips/{ids[1]}" in page, "ต้องกระโดดไปดูใบซ้ำอีกใบได้"


def _plate_norm() -> str:
    from ocrslip.normalize import norm_plate

    return norm_plate(PLATE)
