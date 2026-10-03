"""Normalize Thai data into a canonical form, used both for accuracy scoring and for search.

The goal is to collapse "values a human reads as the same" into one identical string:
'080-000-0000' and '0800000000', or 'นายสมชาย' ("Mr. Somchai") and 'สมชาย' ("Somchai").
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")

TITLES = ("นางสาว", "น.ส.", "นส.", "นาย", "นาง", "คุณ", "ด.ช.", "ด.ญ.", "mr.", "mrs.", "ms.")

PROVINCES = ("กรุงเทพมหานคร", "กรุงเทพฯ", "กรุงเทพ", "กทม.", "กทม", "อยุธยา",
             "พระนครศรีอยุธยา", "นนทบุรี", "ปทุมธานี", "สมุทรปราการ", "ชลบุรี",
             "เบตง")

# Plate province choices for the entry form dropdown. Distinct from PROVINCES above,
# which is a list of "words that may come attached to a plate" for split_province to
# strip off, not a list of actual provinces.
# A dropdown rather than free text, because hand-typing yields "ปทุม" / "ปทุมธานี " /
# "กรุงเทพฯ" — three spellings of two provinces, none of which find each other on search.
PROVINCE_CHOICES = (
    "กรุงเทพมหานคร", "กระบี่", "กาญจนบุรี", "กาฬสินธุ์", "กำแพงเพชร", "ขอนแก่น",
    "จันทบุรี", "ฉะเชิงเทรา", "ชลบุรี", "ชัยนาท", "ชัยภูมิ", "ชุมพร", "เชียงราย",
    "เชียงใหม่", "ตรัง", "ตราด", "ตาก", "นครนายก", "นครปฐม", "นครพนม", "นครราชสีมา",
    "นครศรีธรรมราช", "นครสวรรค์", "นนทบุรี", "นราธิวาส", "น่าน", "บึงกาฬ", "บุรีรัมย์",
    "เบตง", "ปทุมธานี", "ประจวบคีรีขันธ์", "ปราจีนบุรี", "ปัตตานี",
    "พระนครศรีอยุธยา", "พะเยา", "พังงา", "พัทลุง", "พิจิตร", "พิษณุโลก",
    "เพชรบุรี", "เพชรบูรณ์", "แพร่", "ภูเก็ต",
    "มหาสารคาม", "มุกดาหาร", "แม่ฮ่องสอน", "ยโสธร", "ยะลา", "ร้อยเอ็ด", "ระนอง",
    "ระยอง", "ราชบุรี", "ลพบุรี", "ลำปาง", "ลำพูน", "เลย", "ศรีสะเกษ", "สกลนคร",
    "สงขลา", "สตูล", "สมุทรปราการ", "สมุทรสงคราม", "สมุทรสาคร", "สระบุรี", "สระแก้ว",
    "สิงห์บุรี", "สุโขทัย", "สุพรรณบุรี", "สุราษฎร์ธานี", "สุรินทร์", "หนองคาย",
    "หนองบัวลำภู", "อ่างทอง", "อำนาจเจริญ", "อุดรธานี", "อุตรดิตถ์", "อุทัยธานี",
    "อุบลราชธานี",
)
# "เบตง" (Betong) appears in the list above even though it is not a province: plates
# issued by the Betong district branch are printed "เบตง", not "ยะลา" (Yala), the only
# place in the country that works this way. This list holds "text that appears on the
# plate", not "names of administrative divisions" — so it belongs here, and must not be
# dropped when reconciling against the 77 provinces.

BRAND_ALIASES = {
    "toyota": ("toyota", "โตโยต้า", "โตโยตา", "toyata"),
    "isuzu": ("isuzu", "izuzu", "อีซูซุ", "อีซุซุ", "isuzo"),
    "honda": ("honda", "ฮอนด้า", "ฮอนดา"),
    "ford": ("ford", "ฟอร์ด"),
    "mazda": ("mazda", "มาสด้า"),
    "nissan": ("nissan", "นิสสัน"),
    "mitsubishi": ("mitsubishi", "มิตซูบิชิ", "มิตซู"),
}

THAI_MONTHS = {
    "มกราคม": 1, "ม.ค.": 1, "มค": 1,
    "กุมภาพันธ์": 2, "ก.พ.": 2, "กพ": 2,
    "มีนาคม": 3, "มี.ค.": 3, "มีค": 3,
    "เมษายน": 4, "เม.ย.": 4, "เมย": 4,
    "พฤษภาคม": 5, "พ.ค.": 5, "พค": 5,
    "มิถุนายน": 6, "มิ.ย.": 6, "มิย": 6,
    "กรกฎาคม": 7, "ก.ค.": 7, "กค": 7,
    "สิงหาคม": 8, "ส.ค.": 8, "สค": 8,
    "กันยายน": 9, "ก.ย.": 9, "กย": 9,
    "ตุลาคม": 10, "ต.ค.": 10, "ตค": 10,
    "พฤศจิกายน": 11, "พ.ย.": 11, "พย": 11,
    "ธันวาคม": 12, "ธ.ค.": 12, "ธค": 12,
}


def _base(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFC", str(text)).translate(THAI_DIGITS)
    return re.sub(r"\s+", " ", text).strip()


def norm_name(text: str | None) -> str:
    """Strip the honorific and surplus whitespace, leaving a lowercased given + family name"""
    s = _base(text).lower()
    for t in TITLES:
        if s.startswith(t):
            s = s[len(t) :].strip()
            break
    return re.sub(r"\s+", " ", s).strip()


def norm_phone(text: str | None) -> str:
    """Keep digits only, and rewrite +66xxxxxxxxx as 0xxxxxxxxx"""
    d = re.sub(r"\D", "", _base(text))
    if d.startswith("66") and len(d) == 11:
        d = "0" + d[2:]
    return d


def split_province(text: str | None) -> tuple[str, str | None]:
    """Split the province off a plate. Returns (plate, province|None)."""
    s = _base(text)
    for p in sorted(PROVINCES, key=len, reverse=True):
        if p in s:
            return s.replace(p, "").strip(" -."), p
    return s, None


def norm_plate(text: str | None) -> str:
    """Strip spaces, dashes, dots and the province off a plate, leaving letters+digits joined"""
    s, _ = split_province(text)
    return re.sub(r"[\s\-\.]", "", s).lower()


def norm_province(text: str | None) -> str:
    """Collapse the many spellings of a province (กทม. / กรุงเทพ / กรุงเทพมหานคร) into one"""
    s = _base(text).lower().rstrip(".")
    if s in {"กทม", "กรุงเทพ", "กรุงเทพฯ", "กรุงเทพมหานคร", "bangkok", "bkk"}:
        return "กรุงเทพ"
    if s in {"อยุธยา", "พระนครศรีอยุธยา"}:
        return "อยุธยา"
    return s


def norm_brand(text: str | None) -> str:
    """Map a brand onto its canonical form (โตโยต้า -> toyota) and drop model and colour"""
    s = _base(text).lower()
    for canonical, aliases in BRAND_ALIASES.items():
        if any(a in s for a in aliases):
            return canonical
    return s


def norm_cartype(text: str | None) -> str:
    s = _base(text).lower()
    return {"อื่น": "อื่นๆ", "อื่นๆ": "อื่นๆ", "อื่น ๆ": "อื่นๆ"}.get(s, s)


def parse_date(text: str | None) -> dt.date | None:
    """Accepts 26/9/69, 26/09/2569, 26 ก.ย. 69, 26 กันยายน 2569, 2026-09-26"""
    s = _base(text)
    if not s:
        return None

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return _mk(y, mo, d)

    m = re.match(r"^(\d{1,2})\s*[/\-\.]\s*(\d{1,2})\s*[/\-\.]\s*(\d{2,4})$", s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        return _mk(y, mo, d)

    m = re.match(r"^(\d{1,2})\s+([^\d\s]+)\s*(\d{2,4})?$", s)
    if m:
        d, month_word, y = m.group(1), m.group(2).replace(" ", ""), m.group(3)
        mo = THAI_MONTHS.get(month_word) or THAI_MONTHS.get(month_word.rstrip("."))
        if mo:
            return _mk(int(y) if y else dt.date.today().year + 543, mo, int(d))
    return None


def _mk(year: int, month: int, day: int) -> dt.date | None:
    """Always resolve the year to Gregorian by picking the reading *closest to today*: 69 -> 2026.

    A parking slip is always dated the day it was written, so a year decades away from
    today is not something a person wrote. The previous rule (two-digit year < 50 gets
    +2600, then treat as Buddhist Era and subtract 543) turned the "26" people wrote for
    2026 CE into 2083 — found on more than 300 slips in the real database. The effect was
    that re-photographing one slip counted as a separate parking round, because the dates
    disagreed.

    Only the single closest year is chosen; we deliberately do not walk to the next
    candidate when that date does not exist (29 Feb in a non-leap year). Walking on
    yields a year decades away, which is worse than admitting the date was unreadable:
    a reviewer can fix an empty field, but nobody will ever catch that 2068 is wrong.
    """
    if year < 100:
        cands = [2000 + year, 1900 + year, 2500 + year - 543, 2600 + year - 543]
    else:
        cands = [year, year - 543]  # > 2400 means Buddhist Era, but let closeness to today decide
    year = min(cands, key=lambda y: abs(y - dt.date.today().year))
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def norm_date(text: str | None) -> str:
    d = parse_date(text)
    return d.isoformat() if d else ""


NORMALIZERS = {
    "name": norm_name,
    "tel": norm_phone,
    "date": norm_date,
    "noplate": norm_plate,
    "province": norm_province,
    "brand": norm_brand,
    "typecar": norm_cartype,
    "location": lambda s: _base(s).lower().replace("อาคารจอด", "อาคาร"),
}


def normalize_field(field: str, value) -> str:
    return NORMALIZERS.get(field, _base)(value)
