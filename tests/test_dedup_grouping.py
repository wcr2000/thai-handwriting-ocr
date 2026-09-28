"""การจัดกลุ่มใบซ้ำ — ไม่ต้องใช้ฐานข้อมูล ทดสอบตรรกะ union-find ล้วน ๆ

ที่ต้องเป็น union-find ไม่ใช่ group by: ใบสามใบเกาะกันคนละทางได้ (A-B รูปเดียวกัน,
B-C ทะเบียนเดียวกัน) ถ้าจัดกลุ่มแยกตามคีย์ทีละแบบ กลุ่มเดียวกันจะถูกนับซ้ำสองรอบ
แล้วรายงานจะบอกจำนวนใบเกินเกินจริง
"""

import datetime as dt

from ocrslip.dedup import group_duplicates, keeper_of

TODAY = dt.date(2026, 9, 28)


SLOT = "อาคาร 1 ชั้น 2 c2"


def slip(sid, *, osha=None, plate=None, tel=None, date=TODAY, status="pending", minute=0,
         location=SLOT, car_status="stored"):
    return {
        "id": sid, "osha": osha, "plate_norm": plate, "tel_digits": tel, "deposit_date": date,
        "review_status": status, "superseded_by": None, "uploaded_by": "ก",
        "name": "ทดสอบ", "plate_raw": plate, "tel": tel, "car_status": car_status,
        "location": location,
        "created_at": dt.datetime(2026, 9, 28, 10, minute),
    }


def ids(groups):
    return sorted(sorted(s["id"] for s in g) for g in groups)


def test_same_original_image_is_one_group():
    rows = [slip("a", osha="H1"), slip("b", osha="H1"), slip("c", osha="H2")]
    assert ids(group_duplicates(rows)) == [["a", "b"]]


def test_same_plate_tel_and_date_is_one_group():
    """ใบกระดาษใบเดียวกันถ่ายสองรูป — hash ต่างกันแต่เป็นใบเดียวกัน"""
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001"),
            slip("b", osha="H2", plate="1กก1", tel="0810000001")]
    assert ids(group_duplicates(rows)) == [["a", "b"]]


def test_same_paper_slip_photographed_with_sloppy_spacing_still_groups():
    """OCR อ่านที่จอดใบเดียวกันได้ช่องว่างไม่เท่ากัน ต้องไม่หลุดการจับซ้ำเพราะเรื่องนี้"""
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001", location="อาคาร 1  ชั้น 2 C2"),
            slip("b", osha="H2", plate="1กก1", tel="0810000001", location="อาคาร 1 ชั้น 2 c2")]
    assert ids(group_duplicates(rows)) == [["a", "b"]]


def test_second_round_on_the_same_day_is_not_a_duplicate():
    """เช้าฝาก บ่ายรับ เย็นฝากอีก — ทะเบียน/เบอร์/วันที่ตรงกันหมด แยกได้ด้วยที่จอดที่เปลี่ยนไป

    เคสนี้คือที่วันที่ฝากกันไม่ได้เลย ถ้าตีว่าซ้ำ ใบรอบเย็นจะหลุดออกจากคิว
    กลายเป็นรถจอดอยู่จริงแต่ไม่มีใบ active
    """
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001", location="อาคาร 1 ชั้น 2 c2"),
            slip("b", osha="H2", plate="1กก1", tel="0810000001", location="อาคาร 3 ชั้น 5 a7")]
    assert group_duplicates(rows) == []


def test_slip_of_a_car_already_returned_never_links_to_a_later_one():
    """ใบที่คืนรถไปแล้วปิดรอบของตัวเองแล้ว ใบถัดมาคือรอบใหม่ ต่อให้ที่จอดซ้ำช่องเดิม"""
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001", car_status="returned"),
            slip("b", osha="H2", plate="1กก1", tel="0810000001")]
    assert group_duplicates(rows) == []


def test_same_car_on_a_different_day_is_not_a_duplicate():
    """รถคันเดิมเอามาฝากอีกรอบวันหลัง = การฝากครั้งใหม่ ห้ามตีว่าซ้ำ"""
    rows = [slip("a", osha="H1", plate="1กก1", tel="0810000001", date=TODAY),
            slip("b", osha="H2", plate="1กก1", tel="0810000001",
                 date=TODAY + dt.timedelta(days=30))]
    assert group_duplicates(rows) == []


def test_slips_linked_through_different_keys_land_in_one_group():
    """a-b เกาะกันด้วยรูป, b-c เกาะกันด้วยทะเบียน ทั้งสามต้องอยู่กลุ่มเดียว"""
    rows = [slip("a", osha="H1"),
            slip("b", osha="H1", plate="1กก1", tel="0810000001"),
            slip("c", osha="H2", plate="1กก1", tel="0810000001")]
    assert ids(group_duplicates(rows)) == [["a", "b", "c"]]


def test_missing_plate_or_date_never_links_slips():
    """ใบที่อ่านทะเบียน/วันที่ไม่ออก ต้องไม่ถูกจับคู่กับใบอื่นที่ก็อ่านไม่ออกเหมือนกัน"""
    rows = [slip("a", osha="H1", plate=None, tel=None, date=None),
            slip("b", osha="H2", plate=None, tel=None, date=None),
            slip("c", osha="H3", plate="1กก1", tel="0810000001", date=None)]
    assert group_duplicates(rows) == []


def test_missing_location_never_links_slips():
    """อ่านที่จอดไม่ออก = ไม่มีตัวแยกรอบ ต้องไม่เดาว่าซ้ำ ปล่อยให้คนตรวจดูรูปเอง"""
    rows = [slip("a", osha="H1", plate="1ขข2", tel="0810000002", location=None),
            slip("b", osha="H2", plate="1ขข2", tel="0810000002", location="  ")]
    assert group_duplicates(rows) == []


def test_keeper_is_the_oldest_approved_slip():
    group = [slip("a", minute=0), slip("b", status="approved", minute=5),
             slip("c", status="approved", minute=9)]
    assert keeper_of(group)["id"] == "b"


def test_no_keeper_when_nobody_reviewed_the_group_yet():
    """กลุ่มที่ยังไม่มีใครตรวจต้องไม่มีตัวจริง — ปล่อยให้คนตรวจใบใดใบหนึ่งตามปกติ"""
    assert keeper_of([slip("a"), slip("b", status="rejected")]) is None
