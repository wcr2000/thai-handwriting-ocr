"""The day stamp on the screenshot a driver saves.

This exists so staff can tell at a glance whether the screenshot they are handed really is from
the day it claims. So these tests pin the two properties that make it trustworthy: the colour
must derive from the deposit date (not from today), and the (colour, quote) pair must not repeat
within the window over which somebody might pass off an old screenshot.
"""

import datetime as dt

from ocrslip.daystamp import DAY_COLORS, QUOTES, day_stamp, quote_cycle_days


def test_color_follows_the_weekday_of_the_deposit_date():
    """28/09/2026 is a Monday — the colour must be Monday's, not that of the day the page was opened"""
    s = day_stamp(dt.date(2026, 9, 28))
    assert s["day"] == "จันทร์"
    assert s["ink"] == DAY_COLORS[0][1]


def test_no_date_means_no_stamp():
    """No date means no stamp — falling back to today would make the saved screenshot lie to staff"""
    assert day_stamp(None) is None


def test_datetime_is_accepted_too():
    """Some callers pass a datetime rather than a date — that must give the same day's colour, not an error"""
    d = dt.date(2026, 9, 28)
    assert day_stamp(dt.datetime(2026, 9, 28, 23, 59)) == day_stamp(d)


def test_quote_changes_every_day():
    """The moment the quote repeats on consecutive days, yesterday's screenshot passes as today's"""
    d = dt.date(2026, 1, 1)
    for i in range(400):
        a, b = day_stamp(d + dt.timedelta(days=i)), day_stamp(d + dt.timedelta(days=i + 1))
        assert a["quote"] != b["quote"]


def test_day_and_quote_pair_is_unique_for_a_full_cycle():
    """Colours cycle every 7 days and quotes every 13, so the pair must never repeat across 91 days.

    That window is the entire protection this stamp provides: an old screenshot less than a
    quarter old has the right colour but the wrong quote. The moment the quote count is edited to
    something divisible by 7, the window collapses — and this test fails.
    """
    d = dt.date(2026, 1, 1)
    cycle = 7 * len(QUOTES)
    pairs = {(s["day"], s["quote"]) for s in
             (day_stamp(d + dt.timedelta(days=i)) for i in range(cycle))}
    assert len(pairs) == cycle


def test_team_can_replace_the_quotes():
    """The team can edit the quotes from the settings page and they still rotate daily as before"""
    own = ["หนึ่ง", "สอง", "สาม"]  # "one", "two", "three"
    d = dt.date(2026, 9, 28)
    got = [day_stamp(d + dt.timedelta(days=i), own)["quote"] for i in range(3)]
    assert set(got) == set(own)


def test_empty_quote_list_falls_back_instead_of_crashing():
    """Clearing the quotes entirely must fall back to the set shipped in code, not divide by zero and break the page.

    This is the final page of registration: if it breaks, the car is already parked but there is
    no slip to screenshot.
    """
    assert day_stamp(dt.date(2026, 9, 28), [])["quote"] in QUOTES


def test_cycle_length_warns_about_multiples_of_seven():
    """A quote count divisible by 7 means last week's screenshot checks out on every count.

    This value is shown on the settings page, so whoever edits the quotes sees the consequence
    before they save.
    """
    assert quote_cycle_days(["a"] * 7) == 7
    assert quote_cycle_days(["a"] * 14) == 14
    assert quote_cycle_days(QUOTES) == 91
