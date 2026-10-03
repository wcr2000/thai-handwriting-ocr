"""Day stamp — a colour keyed to the weekday plus a quote that changes daily.

Why it exists: on the way out, drivers typically hold up the screenshot they saved for a
staff member to look at. Judging by eye whether "this screenshot really is from the day
they claim" is hard when all there is to go on is a small date in digits. A full-width
colour band is visible from a distance and can be checked instantly against the sticker
the team posted at that day's parking point.

The colour comes from the weekday of the deposit date, following the traditional Thai
day colours, while the quote comes from the full date. There are 13 quotes, which does
not divide evenly into 7, so the (colour, quote) pair never repeats across 91 days — an
old screenshot passed off as today's will have the right colour but the wrong quote,
provided the two days are less than a quarter apart.

That makes the *number* of quotes a bigger deal than it looks: if the team edits the
quotes from the settings page down to 7 or 14 (both divisible by 7), the quotes line up
with the same colour every week, and last week's screenshot instantly becomes one where
everything checks out. The settings page therefore has to warn about this; see
quote_cycle_days().

A limitation worth stating plainly: this is an eyeball check, not a real gate. A
screenshot can always be doctored in a photo editor. The real check is still looking the
slip up in the system by reference number or plate.

The "day" key is still returned even though the driver's screenshot no longer prints the
weekday as text — it is what lets the tests confirm the colour really derives from the
weekday of the given date rather than from the day the page was opened.
"""

import datetime as dt
import math
from collections.abc import Sequence

# Traditional Thai day colours, stored as pairs: the pale tint makes the band, the dark
# shade makes the border and the text. This keeps the text on the band legible on a screen
# dimmed right down in the middle of a car park.
DAY_COLORS: list[tuple[str, str, str]] = [
    # (day name, dark shade, pale tint), ordered by Python's weekday(), i.e. Monday = 0
    ("จันทร์", "#a67c00", "#fdf3d0"),
    ("อังคาร", "#c2185b", "#fde3ee"),
    ("พุธ", "#2e7d32", "#e2f5e6"),
    ("พฤหัสบดี", "#d2691e", "#fdeadb"),
    ("ศุกร์", "#1565c0", "#e1effb"),
    ("เสาร์", "#6a3fbf", "#eee6fb"),
    ("อาทิตย์", "#c62828", "#fde6e6"),
]

QUOTES: list[str] = [
    "ใจใสใจสบาย ทำอะไรก็สำเร็จ",
    "ถ้าใจอยู่ฐานที่ 7 ทำอะไรก็สำเร็จอย่างสบาย ๆ",
    "หยุดคือตัวสำเร็จ",
    "ใจหยุดนิ่ง ทุกสิ่งลงตัว",
    "เริ่มต้นวันด้วยใจที่ผ่องใส",
    "ทำดีวันนี้ ได้ดีทุกวัน",
    "ใจเย็น ๆ แล้วทุกอย่างจะง่ายขึ้น",
    "ยิ้มได้ทุกวัน ใจก็ใสทุกวัน",
    "ให้ด้วยใจ ได้บุญทุกครั้ง",
    "ขับขี่ปลอดภัย ใจอยู่กับตัว",
    "ใจสว่าง ทางก็สว่าง",
    "ความดีที่ทำวันนี้ คุ้มครองเราทุกวัน",
    "รอสักนิด ใจสบายกว่าเดิม",
]


def day_stamp(d: dt.date | dt.datetime | None,
              quotes: Sequence[str] = QUOTES) -> dict[str, str] | None:
    """Return that day's stamp. No date means no stamp — never fall back to today.

    Falling back to today when the date is missing would make the saved screenshot lie to
    staff about the slip being deposited today, which is worse than having no colour band
    at all.

    quotes is injectable so the team can edit the quotes from the settings page without a
    code change — hence this function knows nothing about the database, and the caller is
    responsible for supplying the quotes.
    """
    if d is None:
        return None
    if isinstance(d, dt.datetime):
        d = d.date()
    name, ink, wash = DAY_COLORS[d.weekday()]
    return {
        "day": name,
        "ink": ink,
        "wash": wash,
        # toordinal() advances by 1 per day, so consecutive days can never share a quote
        "quote": (quotes or QUOTES)[d.toordinal() % len(quotes or QUOTES)],
    }


def quote_cycle_days(quotes: Sequence[str]) -> int:
    """How many days before the (colour, quote) pair repeats — the LCM of 7 and the quote count.

    This number is the entire detection window the colour band buys you: the maximum age
    of an old screenshot it can still catch. Shown on the settings page so whoever is
    editing the quotes sees the consequence of what they just did before they save.
    """
    return math.lcm(7, len(quotes)) if quotes else 0
