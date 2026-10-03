"""Claiming slips in the queue — three people reviewing at once must never get the same slip.

Previously next_in_queue() handed the same head-of-line slip to everyone, and the "next slip"
button followed an id computed at render time, so everybody walked the queue along exactly the
same path.

Needs a real Postgres: what is under test is FOR UPDATE SKIP LOCKED and the claim-lease
condition, neither of which can be mocked. See tests/conftest.py for how to run it.
"""

import threading

import pytest

from conftest import TEST_SCHEMA, needs_db

pytestmark = needs_db


def claimed_id(client) -> str | None:
    """Press "next slip" and return the id that was claimed (None = the queue is empty)"""
    r = client.get("/review/next", follow_redirects=False)
    assert r.status_code == 303, r.status_code
    loc = r.headers["location"]
    return None if loc == "/review" else loc.rsplit("/", 1)[-1]


def claim_row(slip_id: str) -> dict:
    from ocrslip.db import connect

    with connect() as conn:
        return conn.execute(
            f"SELECT claimed_by, claimed_name, claimed_at, review_status"
            f" FROM {TEST_SCHEMA}.slips WHERE id = %s", (slip_id,)
        ).fetchone()


def test_two_reviewers_get_different_slips(make_slips, worker):
    """The core property: two people asking for a slip must get different slips"""
    make_slips(3)
    a, b = worker("admin"), worker("staff")
    assert claimed_id(a) != claimed_id(b)


APPROVE_FORM = {"name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
                "province": "กรุงเทพ", "brand": "รีโว่", "typecar": "เก๋ง",
                "location": "อาคาร 1", "date": "2026-09-26", "reviewed_by": "krit"}


def approve(client, slip_id: str):
    return client.post(f"/review/{slip_id}/approve", data=APPROVE_FORM,
                       follow_redirects=False)


def test_two_reviewers_drain_the_queue_without_doing_the_same_slip_twice(make_slips, worker):
    """Model the real workflow: claim -> approve -> ask for the next, two people alternating until the queue empties.

    Merely looping on "ask for the next" without approving would never empty the queue, because
    each new request releases the previous slip back into it (one person holds one slip at a time).
    Approval is the only thing that removes a slip from the pile.
    """
    ids = make_slips(6)
    a, b = worker("admin"), worker("staff")
    done: list[str] = []
    for _ in range(20):  # a guard against an infinite loop if the logic breaks
        progressed = False
        for client in (a, b):
            got = claimed_id(client)
            if got is None:
                continue
            assert got not in done, f"slip {got} was served again after being reviewed"
            approve(client, got)
            done.append(got)
            progressed = True
        if not progressed:
            break
    assert sorted(done) == sorted(ids), "every slip must be reviewed, none twice and none missed"


def test_a_reviewer_never_holds_two_slips_at_once(make_slips, worker):
    """One person holds one slip at a time. Otherwise somebody clicking around leaves the queue full of
    claimed slips, and everybody else waits out the leases on slips nobody is actually reviewing.
    """
    from ocrslip.db import connect

    ids = make_slips(3)
    a = worker("admin")
    a.get(f"/review/{ids[2]}")           # opening the last slip directly claims it
    assert claim_row(ids[2])["claimed_by"] is not None

    nxt = claimed_id(a)                  # then ask for the next slip in the normal queue order
    assert nxt == ids[0], "must get the head of the queue"
    assert claim_row(ids[2])["claimed_by"] is None, "the previous slip must be released back into the queue"

    with connect() as conn:
        held = conn.execute(
            f"SELECT count(*) c FROM {TEST_SCHEMA}.slips WHERE claimed_by IS NOT NULL"
        ).fetchone()["c"]
    assert held == 1, f"one person must hold one slip, but {held} slips are held"


def test_asking_again_without_approving_returns_the_same_slip(make_slips, worker):
    """Asking for the next slip without approving must return the same slip, not skip onward forever.

    Because releasing the current slip back into the queue and taking the new head of line yields
    the same slip. What matters is that no slip is skipped out of the queue unreviewed.
    """
    ids = make_slips(3)
    a = worker("admin")
    assert claimed_id(a) == ids[0]
    assert claimed_id(a) == ids[0]


def test_expired_claim_goes_back_into_the_queue(make_slips, worker):
    """Somebody claims a slip and closes the tab: it has to return to the queue by itself, not wait forever for an admin"""
    from ocrslip.db import connect

    ids = make_slips(1)
    a, b = worker("admin"), worker("staff")
    assert claimed_id(a) == ids[0]
    assert claimed_id(b) is None, "while the lease stands, nobody else may get this slip"

    with connect() as conn:  # backdate the claim past its lease
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips"
                     f" SET claimed_at = now() - interval '999 min' WHERE id = %s", (ids[0],))
        conn.commit()
    assert claimed_id(b) == ids[0]


def test_opening_a_slip_claims_it(make_slips, worker):
    """Clicking through from the queue list must also claim, or two people clicking the same row still collide"""
    ids = make_slips(2)
    a, b = worker("admin"), worker("staff")
    a.get(f"/review/{ids[1]}")
    assert claim_row(ids[1])["claimed_by"] is not None
    assert claimed_id(b) == ids[0], "the other person must get a remaining slip, not the one already open"


def test_opening_someone_elses_slip_warns_but_does_not_block(make_slips, worker):
    """A claim is advisory, not a lock: the slip can still be opened, but the holder must be made visible"""
    ids = make_slips(2)
    a, b = worker("admin"), worker("staff")
    held = claimed_id(a)

    page = b.get(f"/review/{held}")
    assert page.status_code == 200
    assert "กำลังตรวจใบนี้อยู่" in page.text
    assert "/review/next" in page.text, "there must be a route to request another free slip"
    assert claim_row(held)["claimed_by"] is not None


def test_approve_hands_out_a_freshly_claimed_slip(make_slips, worker):
    """After approving, the next slip must be freshly claimed, not the id embedded when the page loaded"""
    ids = make_slips(3)
    a, b = worker("admin"), worker("staff")
    mine = claimed_id(a)
    theirs = claimed_id(b)

    r = a.post(f"/review/{mine}/approve", follow_redirects=False, data={
        "name": "สมหมาย ทดสอบ", "tel": "0810000044", "noplate": "กก1234",
        "province": "กรุงเทพ", "brand": "รีโว่", "typecar": "เก๋ง",
        "location": "อาคาร 1", "date": "2026-09-26", "reviewed_by": "krit"})
    assert r.headers["location"] == "/review/next"

    nxt = claimed_id(a)
    assert nxt not in (mine, theirs), "must be neither an already-reviewed slip nor one a colleague holds"
    assert claim_row(mine)["review_status"] == "approved"
    assert claim_row(mine)["claimed_by"] is None, "a slip that has left the queue must be held by nobody"


@pytest.mark.parametrize("n_workers", [6])
def test_parallel_claims_never_hand_out_the_same_slip(make_slips, worker, n_workers):
    """Genuinely simultaneous requests — the case FOR UPDATE SKIP LOCKED exists to guard.

    The claimed_at condition excludes slips already claimed and committed, but cannot see a
    transaction still in flight. Without SKIP LOCKED, two requests milliseconds apart get the
    same slip.
    """
    make_slips(n_workers)
    clients = [worker("admin" if i % 2 else "staff") for i in range(n_workers)]
    got: list[str | None] = [None] * n_workers

    def hit(i: int):
        got[i] = claimed_id(clients[i])

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    handed = [g for g in got if g]
    assert len(handed) == len(set(handed)), f"a slip was served more than once: {got}"
    assert len(handed) == n_workers, f"everybody should have been served; got {got}"


def test_recheck_clears_stale_claims(make_slips, worker):
    """A slip pulled back into the queue must not carry its old claim, or nobody can have it for ten minutes"""
    from ocrslip.db import connect
    from ocrslip.recheck import send_back

    ids = make_slips(1)
    a = worker("admin")
    assert claimed_id(a) == ids[0]

    with connect() as conn:  # model a slip that got approved while a claim was still outstanding
        conn.execute(f"UPDATE {TEST_SCHEMA}.slips SET review_status='approved',"
                     f" needs_review=false WHERE id=%s", (ids[0],))
        send_back(conn, ids[0])
        conn.commit()

    row = claim_row(ids[0])
    assert row["review_status"] == "pending"
    assert row["claimed_by"] is None and row["claimed_at"] is None
