"""สติกเกอร์ประจำวันบนใบที่ผู้มาจอดแคปเก็บไว้

ของชิ้นนี้มีไว้ให้เจ้าหน้าที่กวาดตาเทียบว่าภาพที่ยื่นมาเป็นของวันที่อ้างจริงไหม
เทสต์จึงคุมสองอย่างที่ทำให้มันเชื่อถือได้: สีต้องมาจากวันที่ฝาก (ไม่ใช่วันนี้)
และคู่ (สี, คำคม) ต้องไม่ซ้ำกันในช่วงเวลาที่คนจะเอาภาพเก่ามาอ้าง
"""

import datetime as dt

from ocrslip.daystamp import DAY_COLORS, QUOTES, day_stamp, quote_cycle_days


def test_color_follows_the_weekday_of_the_deposit_date():
    """28/09/2026 เป็นวันจันทร์ — สีต้องเป็นสีวันจันทร์ ไม่ใช่สีของวันที่เปิดหน้า"""
    s = day_stamp(dt.date(2026, 9, 28))
    assert s["day"] == "จันทร์"
    assert s["ink"] == DAY_COLORS[0][1]


def test_no_date_means_no_stamp():
    """ไม่มีวันที่ ต้องไม่มีสติกเกอร์ — เดาเป็นวันนี้คือโกหกเจ้าหน้าที่ในภาพที่แคปไว้"""
    assert day_stamp(None) is None


def test_datetime_is_accepted_too():
    """บางที่ส่ง datetime มา ไม่ใช่ date — ต้องได้สีของวันเดียวกัน ไม่ใช่พัง"""
    d = dt.date(2026, 9, 28)
    assert day_stamp(dt.datetime(2026, 9, 28, 23, 59)) == day_stamp(d)


def test_quote_changes_every_day():
    """คำคมซ้ำสองวันติดกันเมื่อไหร่ ภาพของเมื่อวานจะอ้างเป็นของวันนี้ได้ทันที"""
    d = dt.date(2026, 1, 1)
    for i in range(400):
        a, b = day_stamp(d + dt.timedelta(days=i)), day_stamp(d + dt.timedelta(days=i + 1))
        assert a["quote"] != b["quote"]


def test_day_and_quote_pair_is_unique_for_a_full_cycle():
    """สีวนทุก 7 วัน คำคมวนทุก 13 วัน คู่ของมันจึงต้องไม่ซ้ำเลยตลอด 91 วัน

    นี่คือทั้งหมดที่สติกเกอร์นี้กันได้ — ภาพเก่าที่อายุไม่ถึงไตรมาสจะ "สีถูกแต่คำผิด"
    ถ้าจำนวนคำคมถูกแก้ให้หารกับ 7 ลงตัวเมื่อไหร่ ช่วงที่กันได้จะหดลงทันที เทสต์นี้จะพัง
    """
    d = dt.date(2026, 1, 1)
    cycle = 7 * len(QUOTES)
    pairs = {(s["day"], s["quote"]) for s in
             (day_stamp(d + dt.timedelta(days=i)) for i in range(cycle))}
    assert len(pairs) == cycle


def test_team_can_replace_the_quotes():
    """ทีมแก้คำคมเองจากหน้าตั้งค่าได้ โดยยังวนเปลี่ยนทุกวันเหมือนเดิม"""
    own = ["หนึ่ง", "สอง", "สาม"]
    d = dt.date(2026, 9, 28)
    got = [day_stamp(d + dt.timedelta(days=i), own)["quote"] for i in range(3)]
    assert set(got) == set(own)


def test_empty_quote_list_falls_back_instead_of_crashing():
    """ล้างคำคมจนหมดต้องกลับไปใช้ชุดที่มากับโค้ด ไม่ใช่หาร 0 แล้วหน้าพัง

    หน้านี้คือหน้าสุดท้ายของการลงทะเบียน ถ้ามันพังคือรถเข้ามาจอดแล้วแต่ไม่มีใบให้แคป
    """
    assert day_stamp(dt.date(2026, 9, 28), [])["quote"] in QUOTES


def test_cycle_length_warns_about_multiples_of_seven():
    """จำนวนคำคมที่หารกับ 7 ลงตัว = ภาพของสัปดาห์ก่อนดูถูกต้องทุกอย่าง

    ค่านี้เอาไปโชว์ในหน้าตั้งค่า คนแก้คำคมจะได้เห็นผลก่อนกดบันทึก
    """
    assert quote_cycle_days(["a"] * 7) == 7
    assert quote_cycle_days(["a"] * 14) == 14
    assert quote_cycle_days(QUOTES) == 91
