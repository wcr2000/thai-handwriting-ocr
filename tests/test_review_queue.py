"""The review queue must filter, search and paginate as the table page does, without escaping role boundaries.

The easiest things to get wrong: an empty query turning into LIKE '%%', which matches every row;
and an approver using the search box to peek at slips in the closed piles.
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
        assert r.status_code == 303, f"login as {username} failed"
        return c

    return _login


# ---------- build_filters: the filter set shared by every page ----------

def test_empty_query_adds_no_search_condition():
    """An empty or whitespace-only query must not become a LIKE '%%' matching the whole table"""
    for q in ("", "   ", None):
        where, params = build_filters(q=q)
        assert where == "TRUE"
        assert params == {}


def test_search_uses_indexed_norm_columns():
    """Must ILIKE the *_norm columns, which carry the trigram indexes, not the raw columns"""
    where, params = build_filters(q="สมชาย")
    for col in ("name_norm", "plate_norm", "brand_norm"):
        assert f"{col} ILIKE" in where
    for raw in ("name ILIKE", "plate_raw ILIKE", "brand ILIKE"):
        assert raw not in where
    # The query is normalized exactly as the data was on save — the honorific is stripped
    assert build_filters(q="นายสมชาย")[1]["qname"] == "%สมชาย%"


def test_text_query_does_not_search_phone():
    """With no digits at all, the phone condition must be omitted, or tel_digits LIKE '%%' matches every row"""
    where, params = build_filters(q="สมชาย")
    assert "tel_digits" not in where and "qd" not in params
    assert build_filters(q="089")[1]["qd"] == "%089%"


def test_needs_review_is_part_of_shared_filters():
    """The queue's piles must be built from the same build_filters the table page uses"""
    where, params = build_filters(review_status="pending", needs_review=True)
    assert "review_status = %(review_status)s" in where
    assert "needs_review = %(needs_review)s" in where
    assert params == {"review_status": "pending", "needs_review": True}
    # Omitted means no filter (distinct from passing False, which means the fast-pass pile)
    assert "needs_review" not in build_filters(review_status="pending")[0]


# ---------- the web pages ----------

def test_queue_search_narrows_the_pile(login):
    c = login("admin")
    total = c.get("/review?filter=all").text.split("พบ <strong>")[1].split("</strong>")[0]
    miss = c.get("/review?filter=all&q=ไม่มีใบนี้แน่นอนxyz")
    assert miss.status_code == 200
    assert "พบ <strong>0</strong>" in miss.text
    assert "ไม่มีใบในกองนี้ที่ตรงกับคำค้น" in miss.text
    # An empty query must give the same count, neither losing nor gaining rows
    assert f"พบ <strong>{total}</strong>" in c.get("/review?filter=all&q=").text


def test_queue_is_paged_not_capped_at_one_screen(login):
    """One page must hold at most 50 rows, and page 2 must open (the old version hard-stopped at 200 slips)"""
    c = login("admin")
    r = c.get("/review?filter=all")
    assert r.text.count('">ตรวจ →</a>') <= 50
    assert c.get("/review?filter=all&page=2").status_code == 200


def test_queue_sort_accepts_columns_and_ignores_junk(login):
    c = login("admin")
    for qs in ("sort=name&dir=asc", "sort=tel&dir=desc", "sort=ไม่มีคอลัมน์นี้&dir=asc"):
        assert c.get(f"/review?filter=all&{qs}").status_code == 200


def test_approver_cannot_search_outside_their_piles(login):
    """An approver may search, but is always forced back to "needs review" when requesting a closed pile"""
    c = login("approve")
    for pile in ("approved", "rejected", "all"):
        html = c.get(f"/review?filter={pile}&q=toyota").text
        # The search box carries the current pile along as a hidden field, whose value is the
        # pile actually in effect
        assert '<input type="hidden" name="filter" value="needs">' in html, f"pile {pile} was not clamped"
        assert "filter=approved" not in html and "filter=rejected" not in html
    assert c.get("/review?filter=quick&q=toyota").status_code == 200


def test_review_detail_still_links_to_next_slip(login):
    """The review page must be able to move to the next slip without pulling the whole pile to count it.

    This used to check a hidden input named next_id, an id computed at render time. That is now
    gone, because it is a snapshot of the queue a moment ago rather than now — the destination is
    /review/next, which claims a slip live at press time.
    """
    import re

    c = login("admin")
    m = re.search(r"/review/([0-9a-f-]{36})", c.get("/review?filter=needs").text)
    if m is None:
        pytest.skip("no slips in the needs-review pile to test against")
    r = c.get(f"/review/{m.group(1)}")
    assert r.status_code == 200
    assert "ใบถัดไป" in r.text
    assert 'name="next_id"' not in r.text, "the next slip id should no longer be embedded in the form"


def test_pile_total_matches_the_tab_counter(login):
    """The "N found" total must match the number on the pile tab — this page takes its total from review_counts instead of recounting"""
    import re

    c = login("admin")
    for pile, label in (("needs", "ต้องตรวจ"), ("quick", "ผ่านเร็ว"),
                        ("approved", "อนุมัติแล้ว"), ("all", "ทั้งหมด")):
        html = c.get(f"/review?filter={pile}").text
        tab = re.search(rf"{label} \((\d+)\)", html)
        found = re.search(r"พบ <strong>(\d+)</strong>", html)
        assert tab and found, f"could not read the number for pile {pile}"
        assert tab.group(1) == found.group(1), f"pile {pile}: the tab says {tab.group(1)} but the total says {found.group(1)}"


# ---------- keeping uploader and reviewer separate ----------

def test_person_filters_are_separate_columns():
    """Uploader and reviewer must filter on separate columns, not be conflated"""
    where, params = build_filters(uploaded_by="สมชาย", reviewed_by="สมหญิง")
    assert "lower(btrim(uploaded_by)) = lower(btrim(%(uploaded_by)s))" in where
    assert "lower(btrim(reviewed_by)) = lower(btrim(%(reviewed_by)s))" in where
    assert params == {"uploaded_by": "สมชาย", "reviewed_by": "สมหญิง"}


def test_person_filter_ignores_blank_and_sentinel():
    """An empty name or the sentinel value must not become a condition, or the results come back inexplicably empty"""
    for bad in ("", "   ", None, "__new__", "​"):
        where, params = build_filters(uploaded_by=bad, reviewed_by=bad)
        assert where == "TRUE"
        assert params == {}


def test_reviewed_by_is_sortable():
    """Sorting by reviewer must work, or the clickable column header silently falls back to created_at"""
    from ocrslip.db import SORTABLE

    assert SORTABLE["reviewed_by"] == "reviewed_by"
