"""Schema ของข้อมูลบนใบฝากรถ — ใช้ร่วมกันทั้ง bench และ web app

ออกแบบจาก layout จริงของใบ (ดู example/):
    ชื่อ ....................................................
    เบอร์โทร ..................  วันที่ ....................
    ทะเบียนรถ ................  ยี่ห้อ .....................
    ประเภทรถ  ตู้ ☐  กระบะ ☐  เก๋ง ☐  อื่นๆ ..............
และหลายใบมีข้อความ "อาคารจอด 3 ชั้น 5" เขียนแทรกข้างบน/ข้างล่างฟอร์ม
"""

from __future__ import annotations

FIELDS = ["name", "tel", "date", "noplate", "province", "brand", "typecar", "location"]

CAR_TYPES = ["ตู้", "กระบะ", "เก๋ง", "อื่นๆ"]


def _nullable(kind: str, desc: str) -> dict:
    return {"type": [kind, "null"], "description": desc}


SLIP_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": FIELDS + ["confidence"],
    "properties": {
        "name": _nullable("string", "ชื่อ-นามสกุลผู้ฝากรถ ตามที่เขียนในช่อง 'ชื่อ'"),
        "tel": _nullable("string", "เบอร์โทร เอาเฉพาะตัวเลข ไม่ต้องมีขีดหรือช่องว่าง"),
        "date": _nullable("string", "วันที่ ตามที่เขียนจริงแบบคำต่อคำ เช่น '26/9/69' หรือ '26 กันยายน 2569' ห้ามแปลงเป็นรูปแบบอื่น"),
        "noplate": _nullable("string", "ทะเบียนรถ เฉพาะหมวดอักษร+ตัวเลข ไม่รวมชื่อจังหวัด เช่น 'กก1234', '1ขค5678'"),
        "province": _nullable("string", "จังหวัดที่เขียนต่อท้ายทะเบียน ถ้าไม่ได้เขียนให้เป็น null"),
        "brand": _nullable("string", "ยี่ห้อ/รุ่น/สีรถ ตามที่เขียน เช่น 'toyota vios', 'อีซูซุ'"),
        "typecar": {
            "type": ["string", "null"],
            "enum": CAR_TYPES + [None],
            "description": "ประเภทรถจากช่องที่ถูกติ๊ก/กากบาท เลือกได้ช่องเดียว ถ้าไม่มีช่องไหนถูกติ๊กให้เป็น null",
        },
        "location": _nullable("string", "ตำแหน่งที่จอดรถ ที่เขียนแทรกนอกช่องฟอร์ม เช่น 'อาคาร 3 ชั้น 5 c2' ถ้าไม่มีให้ null"),
        "confidence": {
            "type": "object",
            "additionalProperties": False,
            "required": FIELDS,
            "properties": {
                f: {"type": "number", "description": f"ความมั่นใจของ {f} ระหว่าง 0 ถึง 1"}
                for f in FIELDS
            },
        },
    },
}

PROMPT = """คุณคืออ่านลายมือภาษาไทยจาก "ใบฝากรถช่วงน้ำท่วม" ที่เขียนด้วยปากกา

ฟอร์มในใบมีโครงแบบนี้:
  ชื่อ ..........................................................
  เบอร์โทร .....................  วันที่ ........................
  ทะเบียนรถ ...................  ยี่ห้อ .........................
  ประเภทรถ  ตู้ ☐  กระบะ ☐  เก๋ง ☐  อื่นๆ .....................

กติกา:
1. อ่านเฉพาะสิ่งที่ "เขียนด้วยลายมือ" ห้ามเอาข้อความที่พิมพ์ไว้ในฟอร์ม (เช่นคำว่า ชื่อ, เบอร์โทร, ยี่ห้อ) มาเป็นคำตอบ
2. ถ้าอ่านไม่ออกจริง ๆ ให้ใส่ null — ห้ามเดาหรือแต่งขึ้นมาเอง
3. เบอร์โทรให้เหลือเฉพาะตัวเลข 10 หลัก ตัดขีด/ช่องว่างออก
4. ทะเบียนรถ ให้แยกชื่อจังหวัด (กรุงเทพ/กทม./อยุธยา ฯลฯ) ออกไปไว้ใน province
5. บางคนเขียนข้อมูลล้นช่องหรือเขียนผิดบรรทัด ให้ดูความหมายเป็นหลักมากกว่าตำแหน่ง
6. typecar ให้ดูว่า "ช่องสี่เหลี่ยมไหนถูกติ๊ก/กากบาท/ขีดทับ" แล้วตอบชื่อช่องนั้น ตอบตามที่ติ๊กจริงเท่านั้น แม้จะดูขัดกับยี่ห้อรถก็ตาม
7. หลายใบมีข้อความบอกที่จอดรถ เช่น "อาคารจอด 3 ชั้น 5" เขียนแทรกอยู่นอกช่องฟอร์ม ให้เก็บไว้ใน location
8. วันที่ให้ตอบตามที่เขียนจริงแบบคำต่อคำ ห้ามแปลง พ.ศ./ค.ศ. เอง

ให้ค่า confidence 0-1 ของแต่ละ field ตามความชัดของลายมือจริง ๆ (ถ้าเดาไม่ออกให้ต่ำกว่า 0.5)"""


# model บางตัว (เช่น claude) ไม่ทำตาม json_schema แล้วตั้งชื่อ key เอง
# map กลับเข้า field มาตรฐาน ไม่งั้นจะถูกนับว่าอ่านไม่ได้ทั้งที่อ่านถูก
ALIASES = {
    "phone": "tel", "tel_no": "tel", "telephone": "tel", "phone_number": "tel", "mobile": "tel",
    "license_plate": "noplate", "plate": "noplate", "license": "noplate",
    "no_plate": "noplate", "registration": "noplate", "car_plate": "noplate",
    "full_name": "name", "owner": "name", "owner_name": "name",
    "car_type": "typecar", "type": "typecar", "vehicle_type": "typecar",
    "car_brand": "brand", "make": "brand",
    "parking": "location", "parking_location": "location", "place": "location",
    "date_raw": "date",
}


def canonicalize(fields: dict) -> dict:
    """เปลี่ยนชื่อ key ที่ model ตั้งเอง ให้กลับมาเป็น field มาตรฐาน"""
    out: dict = {}
    for key, value in (fields or {}).items():
        canon = ALIASES.get(key, key)
        if canon in FIELDS and (out.get(canon) in (None, "")):
            out[canon] = value
    return out
