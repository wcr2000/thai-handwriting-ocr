"""หน้าสรุปภาพรวมต้องไม่พังและต้องไม่โกหก

สองอย่างที่หน้าสรุปพังบ่อยที่สุด และเป็นเหตุผลที่มีไฟล์นี้:

1. หารด้วยศูนย์ตอน DB ยังว่าง — ทุกตัวเลขบนหน้านี้เป็น "กี่ % ของทั้งหมด"
   วันแรกที่ deploy ยังไม่มีใบสักใบ หน้าจะ 500 ทั้งหน้า
2. กราฟที่วาดข้อมูลบางส่วนแต่ดูเหมือนทั้งหมด — แกนวันครอบแค่ 30 วัน
   ถ้าตัวเลขที่ตกขอบหายไปเงียบ ๆ คนอ่านจะสรุปผิดโดยไม่รู้ตัว
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from ocrslip import auth
from ocrslip.web.main import app, axis_ticks

TEST_PW = "pw-for-test"
TODAY = dt.date(2026, 9, 27)


@pytest.fixture
def login(monkeypatch):
    h = auth.hash_password(TEST_PW)
    for prefix, name in (("ADMIN", "admin"), ("USER", "staff"), ("APPROVE", "approve")):
        monkeypatch.setenv(f"{prefix}_USERNAME", name)
        monkeypatch.setenv(f"{prefix}_PASSWORD_HASH", h)
    auth.throttle.fails.clear()
    auth.throttle.locked_until.clear()

    def _login(username: str = "admin") -> TestClient:
        c = TestClient(app)
        r = c.post("/login", data={"username": username, "password": TEST_PW, "next": "/"},
                   follow_redirects=False)
        assert r.status_code == 303, f"ล็อกอิน {username} ไม่ผ่าน"
        return c

    return _login


def stats(**over):
    """ชุดข้อมูลปลอมรูปร่างเดียวกับที่ dashboard_stats คืน — เทสหน้าเว็บโดยไม่แตะ DB จริง"""
    base = {
        "kpi": {
            "total": 100, "approved": 40, "pending": 60, "needs_review": 55, "rejected": 3,
            "stored": 90, "returned": 10, "cost_usd": 0.5, "avg_cost_usd": 0.005,
            "avg_latency": 3.5,
        },
        "by_type": [{"label": "กระบะ", "n": 60}, {"label": "เก๋ง", "n": 40}],
        "by_brand": [{"label": "toyota", "n": 70}],
        "by_location": [{"label": "อาคาร 3", "n": 30}],
        "by_uploader": [{"label": "krit", "n": 55}],
        "by_reviewer": [{"label": "เอย", "n": 30, "rejected": 2}],
        "by_day": [{"label": TODAY - dt.timedelta(days=i), "n": 0, "returned": 0}
                   for i in range(29, -1, -1)],
        "date_health": {"no_date": 0, "odd_date": 0, "older": 0, "in_window": 100},
        "edits": [{"label": "name", "n": 12}],
        "reviewed": 43,
        "reasons": [{"label": "low_confidence", "n": 20}],
    }
    base.update(over)
    return base


def render(login, monkeypatch, payload):
    monkeypatch.setattr("ocrslip.web.main.dashboard_stats", lambda conn: payload)
    monkeypatch.setattr("ocrslip.web.main.connect", lambda: _NullConn())
    return login("admin").get("/dashboard")


class _NullConn:
    """แทน connection จริง — dashboard_stats ถูก monkeypatch ไปแล้วจึงไม่มีใครใช้มัน"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------- หน้าไม่พัง ----------

def test_dashboard_renders(login, monkeypatch):
    r = render(login, monkeypatch, stats())
    assert r.status_code == 200
    assert "สรุปภาพรวม" in r.text


def test_empty_database_does_not_crash(login, monkeypatch):
    """DB ว่างเปล่าวันแรกที่ deploy — ทุกตัวหารเป็นศูนย์หมด ห้าม 500"""
    empty = stats(
        kpi={k: 0 for k in ("total", "approved", "pending", "needs_review", "rejected",
                            "stored", "returned", "cost_usd", "avg_cost_usd", "avg_latency")},
        by_type=[], by_brand=[], by_location=[], by_uploader=[], by_reviewer=[],
        edits=[], reasons=[], reviewed=0,
        date_health={"no_date": 0, "odd_date": 0, "older": 0, "in_window": 0},
    )
    r = render(login, monkeypatch, empty)
    assert r.status_code == 200
    for bad in ("nan", "inf", "Traceback", "Undefined"):
        assert bad not in r.text


def test_no_slip_has_a_date_yet(login, monkeypatch):
    """ทุกวันเป็นศูนย์ แกน Y ต้องยังวาดได้ (เพดานแกนห้ามเป็น 0 แล้วเอาไปหาร)"""
    r = render(login, monkeypatch, stats())
    assert r.status_code == 200
    assert "ใบฝากรถต่อวัน" in r.text


# ---------- หน้าไม่โกหก ----------

def test_slips_outside_the_chart_window_are_reported(login, monkeypatch):
    """ใบที่ตกขอบกราฟต้องถูกบอกจำนวนไว้บนหน้า ไม่ใช่หายไปเงียบ ๆ

    ของจริงตอนเขียนเทสนี้: 2,000 กว่าใบ แต่กราฟครอบแค่ 62% เพราะ OCR อ่านปีผิด
    (เจอ deposit_date ปี 2083) ถ้าไม่บอก คนอ่านจะนึกว่ากราฟคือข้อมูลทั้งหมด
    """
    r = render(login, monkeypatch, stats(
        date_health={"no_date": 273, "odd_date": 480, "older": 7, "in_window": 1241},
    ))
    assert r.status_code == 200
    assert "760" in r.text, "ต้องบอกยอดรวมที่ตกขอบกราฟ (273+480+7)"
    for n in ("273", "480"):
        assert n in r.text, f"ต้องแยกให้เห็นว่า {n} ใบตกขอบด้วยสาเหตุอะไร"


def test_no_callout_when_every_slip_is_in_the_window(login, monkeypatch):
    r = render(login, monkeypatch, stats())
    assert "ใบที่ไม่ได้อยู่ในกราฟนี้" not in r.text


def test_every_chart_has_a_table_view(login, monkeypatch):
    """ค่าที่อ่านได้จากความยาวแถบหรือ hover อย่างเดียว ถือว่าอ่านไม่ได้

    บนมือถือไม่มี hover และแถบสั้น ๆ แยกกันไม่ออก — ตารางคือทางที่อ่านค่าได้เสมอ
    """
    r = render(login, monkeypatch, stats())
    assert r.text.count('class="tableview"') >= 8


def test_peak_value_is_labelled_on_the_chart(login, monkeypatch):
    """แท่งสูงสุดต้องมีตัวเลขติดไว้ (ติดทุกแท่งจะรกจนไม่มีใครอ่าน ติดแท่งเดียวพอ)"""
    days = [{"label": TODAY - dt.timedelta(days=i), "n": 0, "returned": 0} for i in range(29, -1, -1)]
    days[-1] = {"label": TODAY, "n": 1234, "returned": 30}
    r = render(login, monkeypatch, stats(by_day=days))
    assert 'class="peak">1,234' in r.text


def test_reviewer_and_uploader_are_counted_separately(login, monkeypatch):
    """คนอัปกับคนตรวจเป็นคนละบทบาท ลิงก์ที่กดจากแต่ละแถบต้องกรองคนละคอลัมน์"""
    r = render(login, monkeypatch, stats())
    assert "uploaded_by=krit" in r.text
    assert "reviewed_by=" in r.text and "review_status=approved" in r.text


def test_dashboard_is_admin_only(login):
    for who in ("staff", "approve"):
        assert login(who).get("/dashboard", follow_redirects=False).status_code == 403


# ---------- แกน Y ----------

@pytest.mark.parametrize("top", [0, 1, 7, 17, 99, 100, 1217, 5001, 999999])
def test_axis_ticks_cover_the_tallest_bar(top):
    """เพดานแกนต้องไม่ต่ำกว่าค่าสูงสุด ไม่งั้นแท่งจะทะลุออกนอกกรอบกราฟ"""
    ticks = axis_ticks(top)
    assert ticks[-1] >= top
    assert ticks[0] == 0
    assert ticks == sorted(ticks)
    assert len(set(ticks)) == len(ticks), "ห้ามมีป้ายแกนซ้ำกัน"


@pytest.mark.parametrize("top", [0, 1, 3, 7, 1217])
def test_axis_top_is_never_zero(top):
    """เพดานแกนถูกเอาไปหารเสมอ ถ้าเป็น 0 หน้าจะ 500 ทั้งหน้า"""
    assert axis_ticks(top)[-1] > 0


def test_axis_is_not_wastefully_tall():
    """เพดานต้องกระชับพอ ไม่งั้นแท่งจริงจะเตี้ยจนดูเหมือนไม่มีข้อมูล"""
    for top in (17, 100, 1217, 8400):
        assert axis_ticks(top)[-1] <= top * 2


# ---------- SQL จริง: ตัวเลขบนหน้าต้องบวกกันได้ลงตัว ----------

@pytest.fixture
def conn():
    from ocrslip.config import DATABASE_URL
    if not DATABASE_URL:
        pytest.skip("ไม่ได้ตั้ง DATABASE_URL")
    from ocrslip.db import connect
    with connect() as c:
        yield c


def test_date_health_partitions_every_slip(conn):
    """4 ช่องของ date_health ต้องแบ่งใบทุกใบพอดี ห้ามซ้ำ ห้ามตกหล่น

    ถ้าช่วงวันที่ในเงื่อนไขเหลื่อมกันแม้แต่วันเดียว ใบจะถูกนับสองรอบ
    แล้วแถบเตือน "ยังมีอีก N ใบ" จะรายงานเกินจริง
    """
    from ocrslip.config import DB_SCHEMA
    from ocrslip.db import dashboard_stats

    d = dashboard_stats(conn)
    h = d["date_health"]
    total = conn.execute(
        f"SELECT count(*) AS n FROM {DB_SCHEMA}.slips WHERE review_status <> 'rejected'"
    ).fetchone()["n"]
    assert h["no_date"] + h["odd_date"] + h["older"] + h["in_window"] == total


def test_by_day_is_a_continuous_30_day_axis(conn):
    """แกนวันต้องมีครบทุกวันเรียงจากเก่าไปใหม่ วันที่ไม่มีใบก็ต้องมีที่ของมัน

    ของเดิมหยิบเฉพาะวันที่บังเอิญมีใบมาเรียงติดกัน วันว่างจึงหายไปจากแกน
    ทำให้แท่งที่ห่างกันเป็นเดือนดูเหมือนอยู่ติดกัน
    """
    from ocrslip.db import dashboard_stats

    days = [r["label"] for r in dashboard_stats(conn)["by_day"]]
    assert len(days) == 30
    assert days == sorted(days)
    assert all(b - a == dt.timedelta(days=1) for a, b in zip(days, days[1:]))


def test_by_day_only_counts_plausible_dates(conn):
    """ใบที่ OCR อ่านปีผิด (เช่น 2083) ต้องไม่โผล่มาเป็นแท่งในกราฟ 30 วัน"""
    from ocrslip.db import dashboard_stats

    d = dashboard_stats(conn)
    newest = max(r["label"] for r in d["by_day"])
    assert newest <= dt.date.today() + dt.timedelta(days=1)
    assert sum(r["n"] for r in d["by_day"]) <= d["date_health"]["in_window"]
