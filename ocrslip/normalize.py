"""Normalize ข้อมูลไทยให้อยู่ในรูปมาตรฐาน ใช้ทั้งตอนวัด accuracy และตอนค้นหา

เป้าหมายคือทำให้ "ค่าที่มนุษย์เห็นว่าเหมือนกัน" กลายเป็น string เดียวกัน
เช่น '080-000-0000' กับ '0800000000', 'นายสมชาย' กับ 'สมชาย'
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

# ตัวเลือกจังหวัดของทะเบียน สำหรับ dropdown ในฟอร์มขาเข้า — ต่างจาก PROVINCES ข้างบนที่เป็นรายการ
# "คำที่อาจโผล่ติดมากับทะเบียน" ไว้ให้ split_province ตัดออก ไม่ใช่รายการจังหวัดจริง
# ให้เลือกไม่ให้พิมพ์ เพราะพิมพ์เองจะได้ "ปทุม" / "ปทุมธานี " / "กรุงเทพฯ" แล้วค้นไม่เจอ
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
# "เบตง" อยู่ในลิสต์ข้างบนทั้งที่ไม่ใช่จังหวัด: ป้ายทะเบียนที่ออกจากสาขาอำเภอเบตง
# พิมพ์คำว่า "เบตง" ไม่ใช่ "ยะลา" เป็นที่เดียวในประเทศที่เป็นแบบนี้ ลิสต์นี้คือ
# "ข้อความที่อยู่บนป้าย" ไม่ใช่ "ชื่อเขตการปกครอง" จึงต้องมี ห้ามตัดออกตอนเทียบกับ 77 จังหวัด

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
    """ตัดคำนำหน้า + ช่องว่างส่วนเกิน เหลือแค่ชื่อ-นามสกุล ตัวพิมพ์เล็ก"""
    s = _base(text).lower()
    for t in TITLES:
        if s.startswith(t):
            s = s[len(t) :].strip()
            break
    return re.sub(r"\s+", " ", s).strip()


def norm_phone(text: str | None) -> str:
    """เหลือเฉพาะตัวเลข และแปลง +66xxxxxxxxx ให้เป็น 0xxxxxxxxx"""
    d = re.sub(r"\D", "", _base(text))
    if d.startswith("66") and len(d) == 11:
        d = "0" + d[2:]
    return d


def split_province(text: str | None) -> tuple[str, str | None]:
    """แยกชื่อจังหวัดออกจากทะเบียน คืน (ทะเบียน, จังหวัด|None)"""
    s = _base(text)
    for p in sorted(PROVINCES, key=len, reverse=True):
        if p in s:
            return s.replace(p, "").strip(" -."), p
    return s, None


def norm_plate(text: str | None) -> str:
    """ตัดช่องว่าง/ขีด/จุด/ชื่อจังหวัด ออกจากทะเบียน เหลือหมวดอักษร+ตัวเลขติดกัน"""
    s, _ = split_province(text)
    return re.sub(r"[\s\-\.]", "", s).lower()


def norm_province(text: str | None) -> str:
    """จังหวัดที่เขียนได้หลายแบบ (กทม. / กรุงเทพ / กรุงเทพมหานคร) ให้เป็นตัวเดียวกัน"""
    s = _base(text).lower().rstrip(".")
    if s in {"กทม", "กรุงเทพ", "กรุงเทพฯ", "กรุงเทพมหานคร", "bangkok", "bkk"}:
        return "กรุงเทพ"
    if s in {"อยุธยา", "พระนครศรีอยุธยา"}:
        return "อยุธยา"
    return s


def norm_brand(text: str | None) -> str:
    """map ยี่ห้อเข้า canonical (โตโยต้า -> toyota) และตัดรุ่น/สีออก"""
    s = _base(text).lower()
    for canonical, aliases in BRAND_ALIASES.items():
        if any(a in s for a in aliases):
            return canonical
    return s


def norm_cartype(text: str | None) -> str:
    s = _base(text).lower()
    return {"อื่น": "อื่นๆ", "อื่นๆ": "อื่นๆ", "อื่น ๆ": "อื่นๆ"}.get(s, s)


def parse_date(text: str | None) -> dt.date | None:
    """รองรับ 26/9/69, 26/09/2569, 26 ก.ย. 69, 26 กันยายน 2569, 2026-09-26"""
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
    """แปลงปีให้เป็น ค.ศ. เสมอ: 69 -> 2569 -> 2026"""
    if year < 100:
        year += 2500 if year >= 50 else 2600
    if year > 2400:  # พ.ศ.
        year -= 543
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
