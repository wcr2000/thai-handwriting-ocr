"""ทางเข้า "แก้ไขใบที่อนุมัติไปแล้ว" ต้องกดถึงได้จริงจากหน้าที่เจ้าหน้าที่ใช้อยู่

ของเดิม /review/{id}?edit=1 ทำงานได้ครบอยู่แล้ว แต่ไม่มีลิงก์ไหนในระบบชี้ไปเลย
ใบจากฟอร์มขาเข้า (/in) เข้าสถานะ approved ตั้งแต่แรก จึงไม่เคยโผล่ลิงก์ "ตรวจ" ที่ไหน
คนหน้างานเลยต้องพิมพ์ ?edit=1 ต่อท้าย UUID เอง ซึ่งเท่ากับไม่มีฟีเจอร์นี้

เทสต์ชุดนี้จึงยึด "มีลิงก์ให้กด" เป็นของที่ต้องไม่หาย ไม่ใช่แค่ route ตอบ 200
"""

import pytest
from fastapi.testclient import TestClient

from ocrslip import auth
from ocrslip.web.main import app

from conftest import TEST_PW, TEST_SCHEMA, needs_db

pytestmark = needs_db


def _login(username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
               follow_redirects=False)
    assert r.status_code == 303
    return c


@pytest.fixture
def typed_slip(pgenv):
    """ใบที่ผู้มาจอดกรอกเอง — approved ตั้งแต่แรก ไม่มีรูป ไม่มีคะแนน AI

    นี่คือใบกลุ่มที่ต้องกลับมาแก้บ่อยที่สุด (คนพิมพ์เบอร์ตัวเองผิด)
    """
    from ocrslip.db import connect

    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()
    with connect() as conn:
        conn.execute(f"TRUNCATE {TEST_SCHEMA}.slips CASCADE")
        sid = conn.execute(
            f"""INSERT INTO {TEST_SCHEMA}.slips
                (name, name_norm, tel, tel_digits, plate_raw, plate_norm, province,
                 brand, car_type, location, deposit_date,
                 review_status, needs_review, entry_source)
                VALUES ('สมชาย ใจดี', 'สมชายใจดี', '0875000999', '0875000999',
                        '5 ขก 1234', '5ขก1234', 'กรุงเทพ', 'Honda', 'suv',
                        'อาคาร 3 ชั้น 2', current_date, 'approved', false, 'typed')
                RETURNING id::text""").fetchone()["id"]
        conn.commit()
    return sid


def test_slip_page_links_to_the_edit_form(typed_slip):
    """หน้าใบต้องมีปุ่มแก้ไข — เป็นหน้าเดียวที่เจ้าหน้าที่เดินมาถึงจากการค้นหา"""
    html = _login("staff").get(f"/slips/{typed_slip}").text
    assert f"/review/{typed_slip}?edit=1" in html


def test_search_result_links_to_the_edit_form(typed_slip):
    """เบอร์ผิดมักรู้ตอนขาออก (โทรตามไม่ติด) ซึ่งคนอยู่ที่หน้าค้นหา ไม่ใช่หน้าคิวตรวจ"""
    html = _login("staff").get("/api/search?q=5ขก1234").text
    assert f"/review/{typed_slip}?edit=1" in html


def test_table_row_links_to_the_edit_form(typed_slip):
    """แถวในตารางเคยมีลิงก์เฉพาะใบ pending — ใบที่อนุมัติแล้วจึงตัน"""
    html = _login("admin").get("/table").text
    assert f"/review/{typed_slip}?edit=1" in html


def test_edit_form_of_approved_slip_is_editable_not_readonly(typed_slip):
    """เปิดมาทาง ?edit=1 ต้องได้แถบปุ่มบันทึก ไม่ใช่หน้าอ่านอย่างเดียวว่า "ตรวจไปแล้ว" """
    html = _login("staff").get(f"/review/{typed_slip}?edit=1").text
    assert 'class="actbar" hidden' not in html and "<div class=\"actbar\" hidden>" not in html
    # force = ข้าม optimistic lock ได้ เพราะตั้งใจกลับมาแก้ใบที่ตรวจไปแล้ว
    assert 'name="force" value="1"' in html


def test_edit_form_of_typed_slip_has_no_broken_image(typed_slip):
    """ใบกรอกเองไม่มีรูปหลักฐาน ห้ามมี <img> ชี้ไปที่ /image ให้ได้กรอบรูปแตกครึ่งจอ"""
    html = _login("staff").get(f"/review/{typed_slip}?edit=1").text
    assert f"/image/{typed_slip}" not in html
    assert "ความมั่นใจของ AI" not in html
    assert "AI มั่นใจทุกช่อง" not in html


def test_edit_form_keeps_a_car_type_that_is_not_in_the_dropdown(typed_slip):
    """ฟอร์มขาเข้าให้พิมพ์ชนิดรถเองได้ (suv) รายการในหน้าแก้ไขจึงต้องมีค่านั้นด้วย

    ถ้าไม่มี option ให้ selected, <select> จะส่งค่าว่างกลับมา — เปิดมาแก้เบอร์
    แล้วชนิดรถหายไปเงียบ ๆ พร้อมกัน
    """
    html = _login("staff").get(f"/review/{typed_slip}?edit=1").text
    assert '<option value="suv" selected>suv</option>' in html.replace(" >", ">")


def test_saving_fixes_the_field_and_keeps_the_slip_approved(typed_slip):
    """ของจริงที่ต้องได้: เบอร์เปลี่ยน สถานะไม่หลุด และรู้ว่าใครแก้"""
    from ocrslip.db import connect

    c = _login("staff")
    r = c.post(f"/review/{typed_slip}/approve", data={
        "name": "สมชาย ใจดี", "tel": "0875000996", "date": "29/09/2026",
        "noplate": "5 ขก 1234", "province": "กรุงเทพ", "brand": "Honda",
        "typecar": "suv", "location": "อาคาร 3 ชั้น 2",
        "reviewed_by": "__new__", "reviewed_by_new": "เจ้าหน้าที่ ทดสอบ",
        "force": "1",
    }, follow_redirects=False)
    assert r.status_code == 303

    with connect() as conn:
        row = conn.execute(
            f"SELECT tel, tel_digits, review_status FROM {TEST_SCHEMA}.slips WHERE id = %s",
            (typed_slip,)).fetchone()
        edits = conn.execute(
            f"""SELECT field, old_value, new_value, edited_by FROM {TEST_SCHEMA}.slip_edits
                WHERE slip_id = %s""", (typed_slip,)).fetchall()

    assert row["tel"] == "0875000996"
    # ถ้า tel_digits ไม่ตามไปด้วย ค้นด้วยเบอร์ใหม่จะไม่เจอ ซึ่งคือเหตุผลทั้งหมดที่แก้
    assert row["tel_digits"] == "0875000996"
    assert row["review_status"] == "approved"
    assert [(e["field"], e["old_value"], e["new_value"]) for e in edits] == [
        ("tel", "0875000999", "0875000996")]
    assert edits[0]["edited_by"] == "เจ้าหน้าที่ ทดสอบ"


def test_approver_account_still_cannot_reach_the_slip_page(typed_slip):
    """ลิงก์ใหม่ต้องไม่เปิดประตูให้ role approver ซึ่งได้แค่อัปโหลดกับคิวตรวจ"""
    assert _login("approve").get(f"/slips/{typed_slip}").status_code == 403
