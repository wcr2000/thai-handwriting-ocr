"""หน้าเทียบใบซ้ำที่อนุมัติไปแล้ว — ปุ่มเดียวลบถาวร จึงต้องมีเทสต์กันพลาดทุกทาง

สิ่งที่หน้านี้ต้องไม่ทำ สำคัญกว่าสิ่งที่มันทำ:
  * ห้ามลบใบที่ไม่ได้อยู่ในกลุ่มเดียวกับใบที่เลือก (ฟอร์มถูกแก้ได้ทุกเมื่อ)
  * ห้ามให้ใครที่ไม่ใช่ admin เข้าถึง
  * ห้ามหยิบกลุ่มที่ยังไม่มีใครตรวจมาให้ตัดสิน — พวกนั้นระบบจัดการเองตอนอนุมัติ

ต้องใช้ Postgres จริง ดูวิธีรันที่ tests/conftest.py
"""

import pytest

from conftest import TEST_SCHEMA, needs_db

pytestmark = needs_db

FIELDS = {"tel": "0810000044", "province": "กรุงเทพมหานคร", "brand": "รีโว่",
          "car_type": "เก๋ง", "location": "อาคาร 1 ชั้น 2", "deposit_date": "2026-09-28"}


@pytest.fixture
def approved_twins(pgenv):
    """ใบที่อนุมัติแล้ว N ใบของใบกระดาษใบเดียวกัน แต่ AI อ่านชื่อได้ไม่เหมือนกัน"""
    from ocrslip.db import connect
    from ocrslip.normalize import norm_phone, norm_plate

    def _make(names: tuple[str, ...], plate: str = "กก1234",
              status: str = "approved", **over) -> list[str]:
        with connect() as conn:
            ids = []
            for i, name in enumerate(names):
                ids.append(conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slips
                        (name, tel, tel_digits, plate_raw, plate_norm, province, brand,
                         car_type, location, deposit_date, review_status, needs_review,
                         reviewed_by, reviewed_at, car_status, created_at)
                        VALUES (%(name)s, %(tel)s, %(digits)s, %(plate)s, %(pnorm)s,
                                %(province)s, %(brand)s, %(car_type)s, %(location)s,
                                %(deposit_date)s, %(status)s, false, 'krit', now(),
                                %(car_status)s, now() - (%(age)s || ' min')::interval)
                        RETURNING id::text""",
                    {**FIELDS, "name": name, "plate": plate,
                     "digits": norm_phone(FIELDS["tel"]), "pnorm": norm_plate(plate),
                     "status": status, "car_status": "stored", "age": 100 - i, **over},
                ).fetchone()["id"])
            conn.commit()
        return ids

    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.commit()
    return _make


@pytest.fixture
def link_photo(pgenv):
    """ผูกใบหลายใบให้ถือ "รูปต้นฉบับ" ก้อนเดียวกัน (hash ตรงกัน)

    จำเป็นเพราะคีย์ข้อความของ same_slip() บังคับว่าต้องยังไม่คืนรถทั้งคู่ กลุ่มที่มีใบ
    คืนรถไปแล้วจึงเกาะกันได้ทางรูปเท่านั้น — ซึ่งก็ตรงกับของจริง (ไฟล์เดียวกันยิงซ้ำ)
    """
    from ocrslip.db import connect

    def _link(ids: list[str], sha: str = "a" * 64) -> None:
        with connect() as conn:
            for slip_id in ids:
                conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slip_images
                        (slip_id, kind, sha256, bytes) VALUES (%s, 'original', %s, %s)""",
                    (slip_id, sha, b"x"),
                )
            conn.commit()

    return _link


def ids_left() -> set[str]:
    from ocrslip.db import connect

    with connect() as conn:
        return {r["id"] for r in
                conn.execute(f"SELECT id::text AS id FROM {TEST_SCHEMA}.slips").fetchall()}


def test_page_shows_the_conflicting_fields(approved_twins, worker):
    a, b = approved_twins(("นายกิตติ อินเพ็ง", "นายกิตติ วันเพ็ง"))
    html = worker("admin").get("/dups").text

    assert "กลุ่มที่ 1 จาก 1" in html
    assert "นายกิตติ อินเพ็ง" in html and "นายกิตติ วันเพ็ง" in html
    # ชื่อต่างกัน -> แถวชื่อต้องถูกไฮไลต์ ส่วนเบอร์ที่ตรงกันต้องไม่
    assert 'class="clash"' in html
    assert html.count('class="clash"') == 1, "ต้องไฮไลต์เฉพาะช่องที่ขัดกันจริง"


def test_keeping_one_slip_deletes_only_its_twins(approved_twins, worker):
    a, b, c = approved_twins(("ก ก", "ข ข", "ค ค"))
    client = worker("admin")

    r = client.post("/dups/resolve", data={"keep": b, "i": 0}, follow_redirects=False)
    assert r.status_code == 303
    assert ids_left() == {b}


def test_a_slip_outside_the_group_is_never_deleted(approved_twins, worker):
    """ใบของรถคันอื่นต้องรอดทุกกรณี แม้จะอยู่ในฐานเดียวกัน"""
    a, b = approved_twins(("ก ก", "ข ข"))
    outsider = approved_twins(("ค ค",), plate="1กก9999")[0]

    worker("admin").post("/dups/resolve", data={"keep": a, "i": 0}, follow_redirects=False)
    assert outsider in ids_left()
    assert ids_left() == {a, outsider}


def test_forged_form_cannot_delete_across_groups(approved_twins, worker):
    """ฟอร์มส่ง id อะไรมาก็ตาม กลุ่มถูกคำนวณใหม่ฝั่ง server เสมอ"""
    a, b = approved_twins(("ก ก", "ข ข"))
    lonely = approved_twins(("ค ค",), plate="1กก9999")[0]

    # เลือกเก็บใบที่ไม่ได้อยู่ในกลุ่มซ้ำเลย -> ไม่มีกลุ่มให้จัดการ ต้องไม่ลบอะไรทั้งนั้น
    r = worker("admin").post("/dups/resolve", data={"keep": lonely, "i": 0},
                             follow_redirects=False)
    assert r.status_code == 404
    assert ids_left() == {a, b, lonely}


def test_groups_nobody_reviewed_are_not_listed(approved_twins, worker):
    """กลุ่มที่ยังค้างคิวทั้งกลุ่มไม่ใช่งานของหน้านี้ — ระบบถอนออกเองตอนอนุมัติ"""
    approved_twins(("ก ก", "ข ข"), status="pending")
    assert "ไม่มีกลุ่มไหนเหลือให้ตัดสินแล้ว" in worker("admin").get("/dups").text


def test_one_approved_plus_one_pending_is_not_listed(approved_twins, worker):
    """มีใบอนุมัติใบเดียว = ไม่มีอะไรขัดกันในคลัง ใบที่ค้างคิวถูกถอนออกด้วย mark_superseded"""
    approved_twins(("ก ก",))
    approved_twins(("ข ข",), status="pending")
    assert "ไม่มีกลุ่มไหนเหลือให้ตัดสินแล้ว" in worker("admin").get("/dups").text


def test_page_warns_when_the_group_mixes_stored_and_returned(approved_twins, link_photo, worker):
    a = approved_twins(("ก ก",))[0]
    b = approved_twins(("ข ข",), car_status="returned")[0]
    link_photo([a, b])

    html = worker("admin").get("/dups").text
    assert "ไม่มีใบที่ยัง active เหลือในระบบ" in html


def test_same_photo_group_shows_one_image(approved_twins, link_photo, worker):
    """รูป hash ตรงกันทุกใบ = โชว์รูปเดียว ไม่ให้คนไล่ดูรูปเดียวกันสามรูป"""
    ids = approved_twins(("ก ก", "ข ข"))
    link_photo(ids)

    html = worker("admin").get("/dups").text
    assert "ทุกใบในกลุ่มนี้มาจากรูปเดียวกันเป๊ะ" in html
    assert html.count('<img src="/image/') == 1


@pytest.mark.parametrize("who", ["staff", "approve"])
def test_only_admin_can_open_or_resolve(approved_twins, worker, who):
    a, b = approved_twins(("ก ก", "ข ข"))
    client = worker(who)

    assert client.get("/dups").status_code == 403
    assert client.post("/dups/resolve", data={"keep": a, "i": 0},
                       follow_redirects=False).status_code == 403
    assert ids_left() == {a, b}


# ---------- ปุ่มรวบกลุ่มที่ไม่มีอะไรให้ตัดสิน ----------

def test_bulk_keeps_the_oldest_of_every_identical_group(approved_twins, worker):
    """กลุ่มที่ทุกช่องตรงกันหมด เก็บใบเก่าสุดใบเดียว — ผลในคลังเท่าเดิมทุกตัวอักษร"""
    a, b, c = approved_twins(("ก ก", "ก ก", "ก ก"))
    x, y = approved_twins(("ข ข", "ข ข"), plate="1กก9999")

    r = worker("admin").post("/dups/resolve-identical", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dups?done=3"
    assert ids_left() == {a, x}


def test_bulk_never_touches_a_group_with_a_conflict(approved_twins, worker):
    """ชื่ออ่านได้ไม่เหมือนกัน = มีแถวที่ผิดในคลัง ต้องให้คนดูรูป ห้ามปุ่มรวบแตะ"""
    a, b = approved_twins(("นายกิตติ อินเพ็ง", "นายกิตติ วันเพ็ง"))

    r = worker("admin").post("/dups/resolve-identical", follow_redirects=False)
    assert r.headers["location"] == "/dups?done=0"
    assert ids_left() == {a, b}


def test_bulk_skips_groups_that_mix_stored_and_returned(approved_twins, link_photo, worker):
    """ข้อมูลตรงกันหมดแต่คนละสถานะรถ — เลือกผิดใบเปลี่ยนคำตอบว่ารถยังอยู่ไหม ต้องให้คนตัดสิน"""
    a = approved_twins(("ก ก",))[0]
    b = approved_twins(("ก ก",), car_status="returned")[0]
    link_photo([a, b])

    worker("admin").post("/dups/resolve-identical", follow_redirects=False)
    assert ids_left() == {a, b}


def test_bulk_button_only_shows_when_there_is_something_to_collapse(approved_twins, worker):
    approved_twins(("นายกิตติ อินเพ็ง", "นายกิตติ วันเพ็ง"))
    assert "รวบกลุ่มที่ไม่มีอะไรให้ตัดสิน" not in worker("admin").get("/dups").text

    approved_twins(("ข ข", "ข ข"), plate="1กก9999")
    html = " ".join(worker("admin").get("/dups").text.split())
    assert "รวบกลุ่มที่ไม่มีอะไรให้ตัดสินทีเดียว (1 กลุ่ม / 1 ใบ)" in html


def test_bulk_needs_admin(approved_twins, worker):
    a, b = approved_twins(("ก ก", "ก ก"))
    assert worker("staff").post("/dups/resolve-identical",
                                follow_redirects=False).status_code == 403
    assert ids_left() == {a, b}


def test_whitespace_only_difference_is_not_a_conflict(approved_twins, worker):
    """'1ขก1111' กับ '1ขก 1111' คือทะเบียนเดียวกัน ห้ามส่งไปให้คนตัดสิน

    ของจริงที่เจอบนหน้าเว็บ: ใบสองใบมาจากรูปเดียวกัน (hash ตรงกัน) ต่างกันแค่ที่ AI
    ใส่เว้นวรรคในทะเบียนกับเบอร์ไม่เหมือนกัน หน้านี้เคยไฮไลต์ว่า "ขัดกัน" ทั้งที่
    plate_norm/tel_digits ในคลังเท่ากันเป๊ะ — คนเลยต้องมานั่งกดกลุ่มที่ไม่มีอะไรให้ตัดสิน
    """
    a, b = approved_twins(("นภา ทดสอบ", "นภา ทดสอบ"))
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET tel = '0890000127', "
                     f"plate_raw = '1ขก1111' WHERE id = %s", (a,))
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET tel = '089 000 0127', "
                     f"plate_raw = '1ขก 1111' WHERE id = %s", (b,))
        conn.commit()

    html = worker("admin").get("/dups").text
    assert 'class="clash"' not in html, "เว้นวรรคต่างกันไม่ใช่ความขัดแย้ง"
    assert "รวบกลุ่มที่ไม่มีอะไรให้ตัดสินทีเดียว" in html


def test_bulk_collapses_a_whitespace_only_group(approved_twins, worker):
    """และกลุ่มแบบนั้นต้องถูกปุ่มรวบเก็บได้เลย ไม่ใช่ค้างไว้ให้คนกด"""
    a, b = approved_twins(("นภา ทดสอบ", "นภา ทดสอบ"))
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET plate_raw = '1ขก1111' WHERE id = %s", (a,))
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET plate_raw = '1ขก 1111' WHERE id = %s", (b,))
        conn.commit()

    worker("admin").post("/dups/resolve-identical", follow_redirects=False)
    assert ids_left() == {a}, "ต้องเหลือใบเก่าสุดใบเดียว"


def test_a_real_difference_is_still_a_conflict(approved_twins, worker):
    """กันแก้เกิน: ทะเบียนที่ตัวอักษรคนละตัวยังต้องเป็นความขัดแย้งเหมือนเดิม"""
    a, b = approved_twins(("นภา ทดสอบ", "นภา ทดสอบ"))
    from ocrslip.db import connect
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET plate_raw = '1ขก1111' WHERE id = %s", (a,))
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET plate_raw = '1ขข 1111' WHERE id = %s", (b,))
        conn.commit()

    html = worker("admin").get("/dups").text
    assert html.count('class="clash"') == 1
