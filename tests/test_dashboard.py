"""The dashboard must neither break nor lie.

The two ways dashboards break most often, which is why this file exists:

1. Division by zero while the DB is still empty — every figure on this page is a percentage of a
   total, and on deploy day there is not a single slip, so the whole page 500s.
2. A chart drawing part of the data while looking like all of it — the day axis spans only 30
   days, and if what falls outside it vanishes silently, the reader draws the wrong conclusion
   without knowing.
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
        assert r.status_code == 303, f"login as {username} failed"
        return c

    return _login


def stats(**over):
    """A fake dataset shaped exactly like dashboard_stats' return value, so the page can be tested without a real DB"""
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
    """Stands in for a real connection — dashboard_stats is monkeypatched, so nothing uses it"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------- the page does not break ----------

def test_dashboard_renders(login, monkeypatch):
    r = render(login, monkeypatch, stats())
    assert r.status_code == 200
    assert "สรุปภาพรวม" in r.text


def test_empty_database_does_not_crash(login, monkeypatch):
    """An empty DB on deploy day: every divisor is zero, and the page must not 500"""
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
    """With every day at zero, the Y axis must still render (the axis ceiling must not be 0 and then divided by)"""
    r = render(login, monkeypatch, stats())
    assert r.status_code == 200
    assert "ใบฝากรถต่อวัน" in r.text


# ---------- the page does not lie ----------

def test_slips_outside_the_chart_window_are_reported(login, monkeypatch):
    """Slips falling outside the chart must be counted on the page, not vanish silently.

    Production at the time this test was written: over 2,000 slips, of which the chart covered only
    62%, because OCR misread the year (deposit_date values in 2083 were found). Unstated, a reader
    assumes the chart is the whole dataset.
    """
    r = render(login, monkeypatch, stats(
        date_health={"no_date": 273, "odd_date": 480, "older": 7, "in_window": 1241},
    ))
    assert r.status_code == 200
    assert "760" in r.text, "the total falling outside the chart must be stated (273+480+7)"
    for n in ("273", "480"):
        assert n in r.text, f"it must break out why {n} slips fall outside"


def test_no_callout_when_every_slip_is_in_the_window(login, monkeypatch):
    r = render(login, monkeypatch, stats())
    assert "ใบที่ไม่ได้อยู่ในกราฟนี้" not in r.text


def test_every_chart_has_a_table_view(login, monkeypatch):
    """A value readable only from a bar's length or from hover counts as not readable.

    On a phone there is no hover, and short bars cannot be told apart — the table is the way a
    value can always be read.
    """
    r = render(login, monkeypatch, stats())
    assert r.text.count('class="tableview"') >= 8


def test_peak_value_is_labelled_on_the_chart(login, monkeypatch):
    """The tallest bar must carry its number (labelling every bar is so cluttered nobody reads any; one is enough)"""
    days = [{"label": TODAY - dt.timedelta(days=i), "n": 0, "returned": 0} for i in range(29, -1, -1)]
    days[-1] = {"label": TODAY, "n": 1234, "returned": 30}
    r = render(login, monkeypatch, stats(by_day=days))
    assert 'class="peak">1,234' in r.text


def test_reviewer_and_uploader_are_counted_separately(login, monkeypatch):
    """Uploader and reviewer are separate roles, so each bar's link must filter on a different column"""
    r = render(login, monkeypatch, stats())
    assert "uploaded_by=krit" in r.text
    assert "reviewed_by=" in r.text and "review_status=approved" in r.text


def test_dashboard_is_admin_only(login):
    for who in ("staff", "approve"):
        assert login(who).get("/dashboard", follow_redirects=False).status_code == 403


# ---------- the Y axis ----------

@pytest.mark.parametrize("top", [0, 1, 7, 17, 99, 100, 1217, 5001, 999999])
def test_axis_ticks_cover_the_tallest_bar(top):
    """The axis ceiling must not fall below the maximum, or bars punch out of the plot area"""
    ticks = axis_ticks(top)
    assert ticks[-1] >= top
    assert ticks[0] == 0
    assert ticks == sorted(ticks)
    assert len(set(ticks)) == len(ticks), "axis labels must not repeat"


@pytest.mark.parametrize("top", [0, 1, 3, 7, 1217])
def test_axis_top_is_never_zero(top):
    """The axis ceiling is always divided by, so a 0 takes the whole page down with a 500"""
    assert axis_ticks(top)[-1] > 0


def test_axis_is_not_wastefully_tall():
    """The ceiling has to be tight enough, or the real bars are so short they read as no data"""
    for top in (17, 100, 1217, 8400):
        assert axis_ticks(top)[-1] <= top * 2


# ---------- real SQL: the figures on the page have to reconcile ----------

@pytest.fixture
def conn():
    from ocrslip.config import DATABASE_URL
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is not set")
    from ocrslip.db import connect
    with connect() as c:
        yield c


def test_date_health_partitions_every_slip(conn):
    """date_health's 4 buckets must partition every slip exactly — none twice, none missed.

    If the date ranges in those conditions overlap by even one day, slips get counted twice and the
    "N more slips" callout overstates the figure.
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
    """The day axis must hold every day in order, oldest first; a day with no slips still gets its place.

    The previous version strung together only the days that happened to have slips, so empty days
    vanished from the axis and bars months apart appeared adjacent.
    """
    from ocrslip.db import dashboard_stats

    days = [r["label"] for r in dashboard_stats(conn)["by_day"]]
    assert len(days) == 30
    assert days == sorted(days)
    assert all(b - a == dt.timedelta(days=1) for a, b in zip(days, days[1:]))


def test_by_day_only_counts_plausible_dates(conn):
    """A slip whose year OCR misread (2083, say) must not appear as a bar in the 30-day chart"""
    from ocrslip.db import dashboard_stats

    d = dashboard_stats(conn)
    newest = max(r["label"] for r in d["by_day"])
    assert newest <= dt.date.today() + dt.timedelta(days=1)
    assert sum(r["n"] for r in d["by_day"]) <= d["date_health"]["in_window"]
