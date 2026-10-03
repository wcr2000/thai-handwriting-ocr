"""Stop two people reviewing the same slip from overwriting each other (the team reviews three at a time).

Even with claiming in place (tests/test_review_claim.py), collisions still happen: a claim can
expire, somebody can leave a page open for a long time, and anybody can click straight into a
slip somebody else holds. Claiming only makes collisions *rare*; this suite covers the other
half, making a collision harmless.

The symptom seen on the ground was the reviewer field flipping to somebody else's name (the
template lets the DB value beat the locally remembered one) — and pressing approve from there
silently overwrote the first person's work.

Needs a real Postgres, because what is under test is the rowcount of a conditional UPDATE.
See tests/conftest.py for how to run it.
"""

import threading

import pytest

from conftest import TEST_SCHEMA, needs_db

pytestmark = needs_db


def form(reviewer: str, **extra) -> dict:
    return {"name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
            "province": "กรุงเทพ", "brand": "รีโว่ แดง", "typecar": "เก๋ง",
            "location": "อาคาร 1", "date": "2026-09-26",
            "reviewed_by": reviewer, **extra}


def fetch(slip_id: str) -> dict:
    from ocrslip.db import connect, get_slip

    with connect() as conn:
        return get_slip(conn, slip_id)


def test_second_approve_does_not_overwrite_the_first(make_slips, worker):
    """A second person approving a slip a colleague already reviewed must overwrite neither the name, the time, nor the data"""
    slips = make_slips(3)
    a, b = worker("admin"), worker("staff")
    x = slips[0]

    assert a.post(f"/review/{x}/approve", data=form("krit"),
                  follow_redirects=False).status_code == 303
    first = fetch(x)
    assert first["reviewed_by"] == "krit"

    r = b.post(f"/review/{x}/approve", data=form("somchai", name="ชื่อที่จะทับ"),
               follow_redirects=False)
    after = fetch(x)

    assert r.status_code == 200, "must not redirect to the next slip as though the approval succeeded"
    assert after["reviewed_by"] == "krit"
    assert after["reviewed_at"] == first["reviewed_at"]
    assert after["name"] == first["name"]
    assert "ใบนี้ตรวจไปแล้ว" in r.text and "krit" in r.text


def test_blocked_approve_writes_no_audit_row(make_slips, worker):
    """However many times it is resubmitted, slip_edits must not grow, or the "which fields get corrected most" stats are skewed"""
    from ocrslip.db import connect

    slips = make_slips(3)
    a, b = worker("admin"), worker("staff")
    x = slips[0]
    a.post(f"/review/{x}/approve", data=form("krit"), follow_redirects=False)

    def edit_count() -> int:
        with connect() as conn:
            return conn.execute(
                f"SELECT count(*) c FROM {TEST_SCHEMA}.slip_edits WHERE slip_id = %s", (x,)
            ).fetchone()["c"]

    before = edit_count()
    for _ in range(3):
        b.post(f"/review/{x}/approve", data=form("somchai"), follow_redirects=False)
    assert edit_count() == before


def test_reviewed_slip_shows_banner_instead_of_approve_button(make_slips, worker):
    """Following a stale next_id onto a slip a colleague has reviewed must be made obvious, not render as a normal form.

    This is the "reviewer name flips to somebody else's" symptom seen on the ground.
    """
    slips = make_slips(3)
    a, b = worker("admin"), worker("staff")
    x = slips[0]
    a.post(f"/review/{x}/approve", data=form("krit"), follow_redirects=False)

    page = b.get(f"/review/{x}").text
    assert "ใบนี้ตรวจไปแล้ว" in page
    assert 'class="actbar" hidden' in page, "the approve button bar must be hidden"
    # .actbar sets display:flex, which beats the browser's own [hidden] rule. Without this rule
    # the hidden attribute has no real effect and the buttons still show.
    assert ".actbar[hidden] { display:none; }" in page, "a CSS rule that actually hides it is required"
    # No longer links to an id directly: /review/next claims a slip live at press time, so there
    # is no fresh collision with a colleague.
    assert "/review/next" in page, "there must be a route to a next slip nobody has reviewed"
    assert f"/review/{x}?edit=1" in page, "there must be a route to edit it for somebody who really means to"


def test_pending_slip_is_untouched(make_slips, worker):
    """A slip nobody has reviewed must behave exactly as before"""
    slips = make_slips(3)
    b = worker("staff")
    y, z = slips[1], slips[2]

    page = b.get(f"/review/{y}").text
    assert "ใบนี้ตรวจไปแล้ว" not in page
    assert 'class="actbar" hidden' not in page
    select = page.split('id="reviewed_by"')[1].split("</select>")[0]
    assert "selected" not in select, "an unreviewed slip must not preselect anybody's name"

    r = b.post(f"/review/{y}/approve", data=form("somchai"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/review/next"
    assert fetch(y)["reviewed_by"] == "somchai"


def test_edit_query_param_allows_deliberate_fix(make_slips, worker):
    """A deliberate return to edit one's own slip (?edit=1) must still work — not be locked out permanently"""
    slips = make_slips(3)
    a = worker("admin")
    x = slips[0]
    a.post(f"/review/{x}/approve", data=form("krit"), follow_redirects=False)

    page = a.get(f"/review/{x}?edit=1").text
    assert "ใบนี้ตรวจไปแล้ว" not in page
    assert 'name="force" value="1"' in page

    r = a.post(f"/review/{x}/approve", data=form("krit", force="1", name="แก้ทีหลัง"),
               follow_redirects=False)
    assert r.status_code == 303
    assert fetch(x)["name"] == "แก้ทีหลัง"


def test_parallel_approve_has_exactly_one_winner(make_slips, worker):
    """Genuinely simultaneous: two threads, one slip, exactly one must succeed"""
    from ocrslip.db import connect

    z = make_slips(3)[2]
    clients = {"a": worker("admin"), "b": worker("staff")}
    results: dict[str, int] = {}

    def hit(tag: str, who: str):
        results[tag] = clients[tag].post(f"/review/{z}/approve", data=form(who),
                                         follow_redirects=False).status_code

    threads = [threading.Thread(target=hit, args=("a", "krit")),
               threading.Thread(target=hit, args=("b", "somchai"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [200, 303], f"exactly one must succeed; got {results}"
    assert fetch(z)["review_status"] == "approved"
    with connect() as conn:
        editors = conn.execute(
            f"SELECT count(DISTINCT edited_by) c FROM {TEST_SCHEMA}.slip_edits WHERE slip_id = %s",
            (z,),
        ).fetchone()["c"]
    assert editors <= 1, "the audit log must record exactly one editor"
