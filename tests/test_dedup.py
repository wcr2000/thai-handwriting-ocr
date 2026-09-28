"""ใบซ้ำ: อัปรูปเดิมซ้ำต้องไม่ได้ใบใหม่ และคิวต้องไม่จ่ายใบที่ตรวจไปแล้วให้ใครทำอีก

ที่มา: หน้าอัปโหลดไม่ล้างช่องไฟล์หลังอัปสำเร็จ คนที่คิดว่าเมื่อกี้ไม่ติดจึงกดอีกที
ของจริงได้ใบเกินมา 650 ใบ คนตรวจเลยเจอใบที่เพื่อนตรวจไปแล้ววนกลับมาให้ทำซ้ำ
เหมือนงานที่ทำไปไม่ได้บันทึก

ต้องใช้ Postgres จริง — ของที่ทดสอบคือเงื่อนไขใน UPDATE/SELECT ของคิวกับ FK
ON DELETE SET NULL ซึ่ง mock ไม่ได้ ดูวิธีรันที่ tests/conftest.py
"""

import pytest

from conftest import TEST_SCHEMA, needs_db
from test_preprocess import WOOD, fake_photo

pytestmark = needs_db


@pytest.fixture
def photo():
    """รูปถ่ายจำลอง 1 ใบ (ไบต์เดิมทุกครั้ง จึงได้ hash เดิม = จำลองการอัปไฟล์เดิมซ้ำ)"""
    from ocrslip.imageio import encode_jpeg, to_pil

    return encode_jpeg(to_pil(fake_photo(WOOD)), quality=90)


@pytest.fixture
def fake_ocr(monkeypatch):
    """แทน read_slip ไม่ให้เทสต์ยิง model จริง (เสียเงินและช้า) พร้อมนับจำนวนครั้งที่ถูกเรียก"""
    from ocrslip.ocr import OcrResult
    from ocrslip.web import pipeline

    calls = []

    def _read(jpeg, model="fake/model"):
        calls.append(jpeg)
        return OcrResult(
            model="fake/model", latency_s=0.1,
            fields={"name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
                    "province": "กรุงเทพมหานคร", "brand": "รีโว่", "typecar": "เก๋ง",
                    "location": "อาคาร 1 ชั้น 2", "date": "2026-09-28"},
            confidence={"name": 0.95, "tel": 0.95, "noplate": 0.95},
        )

    monkeypatch.setattr(pipeline, "read_slip", _read)
    return calls


def rows(sql: str, params=()) -> list[dict]:
    from ocrslip.db import connect

    with connect() as conn:
        return conn.execute(sql.replace("{s}", TEST_SCHEMA), params).fetchall()


def clear():
    from ocrslip.db import connect

    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        conn.commit()


# ---------- ด่านกันอัปซ้ำ (ก่อนยิง model) ----------

def test_uploading_the_same_photo_twice_keeps_one_slip(pgenv, photo, fake_ocr):
    """หัวใจของการแก้: รูปเดิมยิงซ้ำต้องไม่ได้ใบใหม่ และต้องไม่เสียค่า OCR รอบสอง"""
    from ocrslip.db import connect
    from ocrslip.web.pipeline import ingest

    clear()
    with connect() as conn:
        first = ingest(conn, photo, uploaded_by="ก")
    assert first["ok"] and not first.get("duplicate_of")

    with connect() as conn:
        second = ingest(conn, photo, uploaded_by="ข")

    assert second["ok"]
    assert second["duplicate_of"], "รูปเดิมยิงซ้ำต้องถูกจับได้"
    assert second["id"] == first["id"], "ต้องชี้กลับไปใบเดิม ไม่ใช่สร้างใบใหม่"
    assert len(rows("SELECT id FROM {s}.slips")) == 1
    assert len(fake_ocr) == 1, "ครั้งที่สองต้องไม่ยิง model เลย"


def test_duplicate_upload_says_which_slip_it_matched(pgenv, photo, fake_ocr):
    """คนที่อัปซ้ำต้องได้รู้ว่าใบเดิมอยู่สถานะไหน ไม่ใช่แค่บอกว่า 'ซ้ำ'"""
    from ocrslip.db import connect
    from ocrslip.web.pipeline import ingest

    clear()
    with connect() as conn:
        ingest(conn, photo, uploaded_by="ก")
        twin = ingest(conn, photo, uploaded_by="ข")["duplicate_of"]

    assert twin["review_status"] == "pending"
    assert twin["uploaded_by"] == "ก", "ต้องบอกชื่อคนที่อัปใบเดิม ไม่ใช่คนที่กำลังอัปซ้ำ"


# ---------- ถอนใบซ้ำออกจากคิวตอนอนุมัติ ----------

TWIN = {"name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
        "province": "กรุงเทพมหานคร", "brand": "รีโว่", "typecar": "เก๋ง",
        "location": "อาคาร 1 ชั้น 2", "date": "2026-09-28"}


@pytest.fixture
def twins(pgenv):
    """สร้างใบ pending ที่เป็นใบเดียวกัน N ใบ (ทะเบียน+เบอร์+วันที่ฝากตรงกัน) เก่าไปใหม่"""
    from ocrslip.db import connect
    from ocrslip.normalize import norm_phone, norm_plate

    def _make(n: int = 2, date: str = "2026-09-28") -> list[str]:
        with connect() as conn:
            ids = [
                conn.execute(
                    f"""INSERT INTO {TEST_SCHEMA}.slips
                        (name, tel, tel_digits, plate_raw, plate_norm, deposit_date, location,
                         review_status, needs_review, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', true,
                                now() - (%s || ' min')::interval)
                        RETURNING id::text""",
                    (TWIN["name"], TWIN["tel"], norm_phone(TWIN["tel"]), TWIN["noplate"],
                     norm_plate(TWIN["noplate"]), date, TWIN["location"], 100 - i),
                ).fetchone()["id"]
                for i in range(n)
            ]
            conn.commit()
        return ids

    clear()
    return _make


def approve(client, slip_id: str, **over):
    return client.post(f"/review/{slip_id}/approve",
                       data={**TWIN, "reviewed_by": "krit", **over},
                       follow_redirects=False)


def superseded_by(slip_id: str):
    row = rows("SELECT superseded_by::text AS s FROM {s}.slips WHERE id = %s", (slip_id,))[0]
    return row["s"]


def test_approving_one_slip_pulls_its_twin_out_of_the_queue(twins, worker):
    """สิ่งที่ krit เจอ: ตรวจใบแรกเสร็จ ใบที่สองซึ่งเป็นใบเดียวกันต้องไม่ถูกจ่ายให้ใครอีก"""
    first, second = twins(2)
    client = worker("staff")

    assert approve(client, first).status_code == 303
    assert superseded_by(second) == first

    # คิวว่างแล้ว: /review/next ต้องไม่มีใบให้จอง
    r = client.get("/review/next", follow_redirects=False)
    assert r.headers["location"] == "/review", "คิวต้องไม่จ่ายใบซ้ำให้ตรวจอีก"


def test_superseded_slip_is_visible_in_its_own_pile(twins, worker):
    """ใบซ้ำต้องไม่หายไปเงียบ ๆ — ถ้าตีว่าซ้ำผิด ต้องมีที่ให้เจอ"""
    first, second = twins(2)
    client = worker("staff")
    approve(client, first)

    page = client.get("/review?filter=dup").text
    assert "ซ้ำกับใบที่ตรวจแล้ว" in page
    counts = rows(
        "SELECT count(*) FILTER (WHERE superseded_by IS NOT NULL) AS dup,"
        " count(*) FILTER (WHERE review_status='pending' AND superseded_by IS NULL) AS todo"
        " FROM {s}.slips")[0]
    assert (counts["dup"], counts["todo"]) == (1, 0)


def test_a_whole_pile_of_twins_collapses_in_one_approval(twins, worker):
    """ของจริงมีถึง 9 ใบต่อกลุ่ม — อนุมัติครั้งเดียวต้องเคลียร์ที่เหลือทั้งกอง"""
    ids = twins(5)
    client = worker("staff")
    approve(client, ids[0])

    assert all(superseded_by(i) == ids[0] for i in ids[1:])


def test_same_car_deposited_on_another_day_stays_in_the_queue(twins, worker):
    """รถคันเดิมเอามาฝากอีกรอบ = การฝากครั้งใหม่ ห้ามถอนออกจากคิว"""
    from ocrslip.db import connect

    first, second = twins(2)
    with connect() as conn:  # ใบที่สองเป็นการฝากของเดือนหน้า
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET deposit_date='2026-10-28' WHERE id=%s",
                     (second,))
        conn.commit()

    client = worker("staff")
    approve(client, first)
    assert superseded_by(second) is None


def test_second_round_on_the_same_day_stays_in_the_queue(twins, worker):
    """เช้าฝาก บ่ายรับ เย็นฝากอีก — ใบรอบเย็นห้ามถูกถอนออกจากคิวตอนอนุมัติใบรอบเช้า

    เคสนี้วันที่ฝากกันไม่ได้ (ตรงกันทั้งคู่) ตัวที่แยกออกคือที่จอด เพราะรอบใหม่ได้ช่องจอดใหม่
    ถ้าปล่อยให้ตีว่าซ้ำ รถรอบเย็นจะจอดอยู่จริงแต่ไม่มีใบ active
    """
    from ocrslip.db import connect

    first, second = twins(2)
    with connect() as conn:  # ใบที่สองคือรอบเย็น จอดคนละช่อง
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET location='อาคาร 3 ชั้น 5 c2' WHERE id=%s",
                     (second,))
        conn.commit()

    approve(worker("staff"), first)
    assert superseded_by(second) is None


def test_twin_of_a_car_already_returned_stays_in_the_queue(twins, worker):
    """ใบที่คืนรถไปแล้วปิดรอบของตัวเองแล้ว ใบถัดมาคือรอบใหม่ ต่อให้จอดช่องเดิม"""
    from ocrslip.db import connect

    first, second = twins(2)
    with connect() as conn:  # รอบแรกรับรถกลับไปแล้ว
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET car_status='returned',"
                     f" returned_at=now() WHERE id=%s", (first,))
        conn.commit()

    approve(worker("staff"), first)
    assert superseded_by(second) is None


def test_same_photo_still_collapses_even_after_the_car_went_home(pgenv, photo, fake_ocr):
    """ไฟล์เดียวกันเป๊ะคือของซ้ำแน่นอน เงื่อนไขกันรอบใหม่ต้องไม่ไปกันสาขา hash รูป"""
    from ocrslip.db import connect
    from ocrslip.web.pipeline import ingest

    clear()
    with connect() as conn:
        first = ingest(conn, photo, uploaded_by="ก")["id"]
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET car_status='returned' WHERE id=%s",
                     (first,))
        # ใบที่สองถือรูปเดิม (จำลองของที่ค้างอยู่ก่อนปิดต้นเหตุที่ ingest)
        second = conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slips (plate_raw, review_status, needs_review)
                VALUES ('กก1234', 'pending', true) RETURNING id::text""").fetchone()["id"]
        conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slip_images (slip_id, kind, sha256, bytes)
                SELECT %s, kind, sha256, bytes FROM {TEST_SCHEMA}.slip_images
                 WHERE slip_id = %s AND kind = 'original'""", (second, first))
        from ocrslip.db import mark_superseded
        assert mark_superseded(conn, first) == 1
        conn.commit()

    assert superseded_by(second) == first


def test_rejected_twin_is_left_alone(twins, worker):
    """ใบที่คนตีกลับไปแล้วเป็นงานที่คนทำแล้ว ห้ามสคริปต์ไปทับด้วยการเดา"""
    first, second = twins(2)
    client = worker("staff")
    client.post(f"/review/{second}/reject", data={"reason": "รูปเบลอ", "reviewed_by": "krit"},
                follow_redirects=False)
    approve(client, first)

    row = rows("SELECT review_status, superseded_by FROM {s}.slips WHERE id=%s", (second,))[0]
    assert (row["review_status"], row["superseded_by"]) == ("rejected", None)


def test_deleting_the_real_slip_puts_its_twin_back_in_the_queue(twins, worker):
    """ถ้าใบตัวจริงถูกลบ ใบซ้ำต้องกลับเข้าคิว ไม่ใช่หายไปทั้งคู่ (FK ON DELETE SET NULL)"""
    from ocrslip.db import connect, delete_slip

    first, second = twins(2)
    approve(worker("staff"), first)
    assert superseded_by(second) == first

    with connect() as conn:
        delete_slip(conn, first)
        conn.commit()
    assert superseded_by(second) is None


# ---------- สคริปต์เก็บกวาดของเก่า ----------

def test_dedup_apply_clears_twins_of_already_approved_slips(twins, worker):
    """ของที่ค้างอยู่ก่อนแก้: ใบซ้ำที่มีพี่น้องอนุมัติไปแล้ว ต้องถูกถอนออกจากคิวได้ทีเดียว"""
    from ocrslip.db import connect
    from ocrslip.dedup import group_duplicates, keeper_of, load_slips
    from ocrslip.dedup import mark_superseded

    first, second, third = twins(3)
    # จำลองสภาพก่อนแก้: ใบแรกอนุมัติแล้ว แต่ที่เหลือยังค้างคิว (ไม่ผ่าน hook ตอนอนุมัติ)
    with connect() as conn:
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET review_status='approved',"
                     f" needs_review=false, reviewed_by='krit', reviewed_at=now()"
                     f" WHERE id=%s", (first,))
        conn.commit()

    with connect() as conn:
        groups = group_duplicates(load_slips(conn))
        assert len(groups) == 1 and len(groups[0]) == 3
        assert keeper_of(groups[0])["id"] == first
        assert mark_superseded(conn, first) == 2
        conn.commit()

    assert superseded_by(second) == first and superseded_by(third) == first
