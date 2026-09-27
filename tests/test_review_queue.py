"""คิวตรวจต้องกรอง/ค้น/แบ่งหน้าได้เหมือนหน้าตาราง โดยไม่หลุดขอบเขตของ role

จุดที่พังง่ายที่สุดคือคำค้นว่าง ๆ แล้วกลายเป็น LIKE '%%' ซึ่งแมตช์ทุกแถว
และ approver ที่ใช้คำค้นเพื่อแอบดูใบในกองที่ปิดงานไปแล้ว
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip import auth
from ocrslip.db import build_filters
from ocrslip.web.main import app

TEST_PW = "pw-for-test"


@pytest.fixture
def login(monkeypatch):
    h = auth.hash_password(TEST_PW)
    for prefix, name in (("ADMIN", "admin"), ("USER", "staff"), ("APPROVE", "approve")):
        monkeypatch.setenv(f"{prefix}_USERNAME", name)
        monkeypatch.setenv(f"{prefix}_PASSWORD_HASH", h)
    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()

    def _login(username: str) -> TestClient:
        c = TestClient(app)
        r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303, f"ล็อกอิน {username} ไม่ผ่าน"
        return c

    return _login


# ---------- build_filters: ตัวกรองที่ใช้ร่วมกันทุกหน้า ----------

def test_empty_query_adds_no_search_condition():
    """คำค้นว่าง/มีแต่ช่องว่าง ต้องไม่กลายเป็น LIKE '%%' ที่แมตช์ทั้งตาราง"""
    for q in ("", "   ", None):
        where, params = build_filters(q=q)
        assert where == "TRUE"
        assert params == {}


def test_search_uses_indexed_norm_columns():
    """ต้อง ILIKE คอลัมน์ *_norm ที่มี trigram index ไม่ใช่คอลัมน์ดิบ"""
    where, params = build_filters(q="สมชาย")
    for col in ("name_norm", "plate_norm", "brand_norm"):
        assert f"{col} ILIKE" in where
    for raw in ("name ILIKE", "plate_raw ILIKE", "brand ILIKE"):
        assert raw not in where
    # คำค้นถูก normalize แบบเดียวกับตอนบันทึก — คำนำหน้าถูกตัดทิ้ง
    assert build_filters(q="นายสมชาย")[1]["qname"] == "%สมชาย%"


def test_text_query_does_not_search_phone():
    """ไม่มีตัวเลขเลย ห้ามใส่เงื่อนไขเบอร์ ไม่งั้น tel_digits LIKE '%%' จะแมตช์ทุกแถว"""
    where, params = build_filters(q="สมชาย")
    assert "tel_digits" not in where and "qd" not in params
    assert build_filters(q="089")[1]["qd"] == "%089%"


def test_needs_review_is_part_of_shared_filters():
    """กองในคิวตรวจต้องสร้างจาก build_filters ชุดเดียวกับหน้าตาราง"""
    where, params = build_filters(review_status="pending", needs_review=True)
    assert "review_status = %(review_status)s" in where
    assert "needs_review = %(needs_review)s" in where
    assert params == {"review_status": "pending", "needs_review": True}
    # ไม่ส่งมา = ไม่กรอง (ต่างจากส่ง False ซึ่งหมายถึงกอง "ผ่านเร็ว")
    assert "needs_review" not in build_filters(review_status="pending")[0]


# ---------- หน้าเว็บ ----------

def test_queue_search_narrows_the_pile(login):
    c = login("admin")
    total = c.get("/review?filter=all").text.split("พบ <strong>")[1].split("</strong>")[0]
    miss = c.get("/review?filter=all&q=ไม่มีใบนี้แน่นอนxyz")
    assert miss.status_code == 200
    assert "พบ <strong>0</strong>" in miss.text
    assert "ไม่มีใบในกองนี้ที่ตรงกับคำค้น" in miss.text
    # คำค้นว่างต้องได้จำนวนเท่าเดิม ไม่ใช่หายไปหรือเพิ่มขึ้น
    assert f"พบ <strong>{total}</strong>" in c.get("/review?filter=all&q=").text


def test_queue_is_paged_not_capped_at_one_screen(login):
    """หน้าเดียวต้องไม่เกิน 50 แถว และหน้าที่ 2 ต้องเปิดได้ (ของเดิมตัดตายที่ 200 ใบ)"""
    c = login("admin")
    r = c.get("/review?filter=all")
    assert r.text.count('">ตรวจ →</a>') <= 50
    assert c.get("/review?filter=all&page=2").status_code == 200


def test_queue_sort_accepts_columns_and_ignores_junk(login):
    c = login("admin")
    for qs in ("sort=name&dir=asc", "sort=tel&dir=desc", "sort=ไม่มีคอลัมน์นี้&dir=asc"):
        assert c.get(f"/review?filter=all&{qs}").status_code == 200


def test_approver_cannot_search_outside_their_piles(login):
    """approver ค้นได้ แต่ถูกบังคับกลับมากอง 'ต้องตรวจ' เสมอถ้าขอกองที่ปิดงานแล้ว"""
    c = login("approve")
    for pile in ("approved", "rejected", "all"):
        html = c.get(f"/review?filter={pile}&q=toyota").text
        # ช่องค้นหาพากองที่กำลังดูอยู่ไปด้วยเป็น hidden field — ค่าในนั้นคือกองที่ถูกใช้จริง
        assert '<input type="hidden" name="filter" value="needs">' in html, f"กอง {pile} ไม่ถูก clamp"
        assert "filter=approved" not in html and "filter=rejected" not in html
    assert c.get("/review?filter=quick&q=toyota").status_code == 200


def test_review_detail_still_links_to_next_slip(login):
    """หน้าตรวจต้องรู้ใบถัดไป + จำนวนที่เหลือ โดยไม่ต้องดึงคิวทั้งกองมานับ"""
    import re

    c = login("admin")
    m = re.search(r"/review/([0-9a-f-]{36})", c.get("/review?filter=needs").text)
    if m is None:
        pytest.skip("ไม่มีใบในกอง 'ต้องตรวจ' ให้ทดสอบ")
    r = c.get(f"/review/{m.group(1)}")
    assert r.status_code == 200
    assert 'name="next_id"' in r.text


def test_pile_total_matches_the_tab_counter(login):
    """ยอด 'พบ N ใบ' ต้องตรงกับเลขในแถบกอง — หน้านี้ใช้ยอดจาก review_counts แทนการนับใหม่"""
    import re

    c = login("admin")
    for pile, label in (("needs", "ต้องตรวจ"), ("quick", "ผ่านเร็ว"),
                        ("approved", "อนุมัติแล้ว"), ("all", "ทั้งหมด")):
        html = c.get(f"/review?filter={pile}").text
        tab = re.search(rf"{label} \((\d+)\)", html)
        found = re.search(r"พบ <strong>(\d+)</strong>", html)
        assert tab and found, f"อ่านเลขของกอง {pile} ไม่ได้"
        assert tab.group(1) == found.group(1), f"กอง {pile}: แถบบอก {tab.group(1)} แต่ยอดบอก {found.group(1)}"
