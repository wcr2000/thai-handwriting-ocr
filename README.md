# ระบบ OCR ใบฝากรถน้ำท่วม

ถ่ายรูปใบฝากรถที่เขียนด้วยลายมือ → ให้ LLM อ่าน → คนตรวจยืนยัน → เก็บลง Postgres พร้อมรูปหลักฐาน
→ ตอนเจ้าของมารับรถคืน ค้นแบบ fuzzy ได้แม้สะกดชื่อผิดหรือจำเบอร์ผิดไปบ้าง

## เริ่มใช้งาน

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # ใส่ OPENROUTER_API_KEY และ DATABASE_URL
.venv/bin/python -m ocrslip.db init     # สร้าง schema ocr_dhammakaya
.venv/bin/uvicorn ocrslip.web.main:app --reload
```

> ถ้า shell ของเครื่องมี `OPENROUTER_API_KEY` ตัวเก่าค้างอยู่ ไม่ต้องกังวล — `config.py` ตั้ง
> `override=True` ให้ค่าใน `.env` ของโปรเจกต์ชนะเสมอ

## หน้าจอ

| หน้า | ทำอะไร |
|---|---|
| `/` | อัปโหลด/ถ่ายรูปใบฝากรถ (หลายใบพร้อมกันได้ รองรับ HEIC จาก iPhone) |
| `/review` | คิวตรวจสอบ แยกกองเป็น ต้องตรวจ / ผ่านเร็ว / อนุมัติแล้ว / ตีกลับ |
| `/search` | ค้นหาแบบ fuzzy ด้วยชื่อ เบอร์ หรือทะเบียน |
| `/table` | ตารางข้อมูลทั้งหมด กรอง/เรียง/แบ่งหน้า + ปุ่มโหลด Excel ตามที่กรอง |
| `/dashboard` | สรุปภาพรวม: KPI, ใบต่อวัน, แยกตามประเภท/ยี่ห้อ/ที่จอด, คุณภาพการอ่านของ AI |
| `/slips/{id}` | ดูใบจริง + กดรับรถคืน + ประวัติการแก้ไข |
| `/export.xlsx` | ดาวน์โหลด Excel (รับ query string ชุดเดียวกับ `/table`) |

หน้าตารางและ dashboard render จากฝั่ง server ทั้งหมด — ไม่มี chart library, ไม่โหลดรูปในตาราง,
สรุปตัวเลขทั้งหน้า dashboard ใช้ query รวมไม่กี่ครั้ง จึงเปิดบนมือถือกลางสนามได้

## ทำไมต้องมีคิวตรวจสอบ

OCR ลายมือไทยไม่มีทางแม่น 100% — ทุกใบจึงเข้าสถานะ `pending` ก่อนเสมอ และถูกชู flag อัตโนมัติเมื่อ
model ไม่มั่นใจ, ช่องบังคับว่าง, รูปแบบผิด (เบอร์ไม่ครบ 10 หลัก / วันที่อ่านไม่ออก) หรือซ้ำกับใบที่ยังฝากอยู่
หน้า search จะเห็นเฉพาะใบที่ `approved` แล้วเท่านั้น และทุกการแก้ถูกบันทึกใน `slip_edits`

ตอนทดสอบกับรูปจริง 10 ใบ flag จับได้ว่า AI อ่านตัวเลขทะเบียนใบหนึ่งผิดไปหนึ่งหลัก จนไปซ้ำกับอีกใบ

## ผลวัด accuracy

ดู [bench/report.md](bench/report.md) — เทียบ 11 models × 3 แบบ preprocessing × 10 ใบ (330 calls)

- **ที่เลือกใช้**: `google/gemini-3-flash-preview` + crop → เฉลี่ย 78%, $1.55/1000 ใบ, ~3 วินาทีต่อใบ
- crop กระดาษช่วยจริง (71% vs 69%) แต่ **การเพิ่ม contrast/ลบเงากลับทำให้แย่ลง** (68%)
- ช่องที่พลาดคือ "ชื่อ" เป็นหลัก แต่ผิดแค่ 1-2 ตัวอักษร (`ชื่อ≈` 90%) ซึ่ง fuzzy search ยังหาเจอ
  และคนตรวจแก้ได้เร็ว ส่วนเบอร์โทรอ่านถูก 100%

### ต้นทุนจริง

**ใบละ ~0.051 บาท** (≈ 5 สตางค์) · 1,000 ใบ = ~51 บาท · 10,000 ใบ = ~510 บาท
(วัดจากค่าที่ OpenRouter เรียกเก็บจริง: input ~2,330 tokens + output ~128 tokens ต่อใบ)

ระบบบันทึก `ocr_cost_usd` / `ocr_tokens_in` / `ocr_tokens_out` / `ocr_latency_s` ของทุกใบลง DB
และสรุปยอดรวมเป็นบาทให้ที่หน้า `/dashboard` — ปรับอัตราแลกเปลี่ยนที่ `USD_THB` ใน `.env`

รันใหม่:
```bash
.venv/bin/python bench/run_bench.py    # cache ไว้ รันซ้ำไม่เสียเงินซ้ำ
.venv/bin/python bench/score.py
```

## โครงสร้าง

```
ocrslip/
  imageio.py     อ่านไฟล์จาก magic bytes (ไฟล์ตัวอย่างเป็น HEIC ทั้งหมดแม้นามสกุลจะเป็น .png/.jpg)
  preprocess.py  จับขอบกระดาษด้วย HSV mask -> perspective warp -> หมุนให้แนวนอน
  schema.py      JSON schema + prompt ที่ใช้ทั้ง bench และ production
  ocr.py         เรียก OpenRouter (structured output + fallback ถ้า model ไม่รองรับ)
  normalize.py   normalize ชื่อ/เบอร์/ทะเบียน/ยี่ห้อ/วันที่ (พ.ศ. -> ค.ศ., เลขไทย -> อารบิก)
  review.py      เกณฑ์ว่าใบไหนต้องให้คนตรวจ
  search.py      trigram + levenshtein ใน Postgres แล้ว rerank ด้วย rapidfuzz
  db.py          schema init + queries
  web/           FastAPI + Jinja2 + HTMX
bench/           ชุดวัด accuracy (run_bench.py -> score.py -> report.md)
db/schema.sql    ตาราง slips / slip_images / slip_edits
example/         ชุดทดสอบ: รูปใบฝากรถ + label.json (ground truth)
                 **ไม่ถูก commit ขึ้น git** เพราะเป็นรูปและข้อมูลส่วนตัวของคนจริง
```

## สิทธิ์การใช้งาน (2 role)

ล็อกอินด้วยบัญชีคงที่ 2 บัญชีที่ตั้งใน `.env` — ไม่มีตาราง user ในฐานข้อมูล เพราะงานนี้มีผู้ใช้ไม่กี่คน

| ทำอะไรได้ | `user` (เจ้าหน้าที่หน้างาน) | `admin` (ผู้ดูแล) |
|---|---|---|
| อัปโหลด + ตรวจ + อนุมัติ | ✅ | ✅ |
| ค้นหา / เปิดดูใบ | ✅ | ✅ |
| กดรับรถคืน | ❌ | ✅ |
| ตีกลับใบ | ❌ | ✅ |
| ตารางข้อมูล / dashboard / โหลด Excel | ❌ | ✅ |

เหตุผลที่ให้ `user` อนุมัติได้: คนถ่ายรูปคือคนที่ยืนอยู่หน้าเจ้าของรถ ตรวจได้แม่นกว่าคนที่มาดูทีหลัง
และถ้าบังคับให้ admin อนุมัติทุกใบจะเป็นคอขวดตอนรถเข้าพร้อมกันหลายคัน
ส่วนสิ่งที่ย้อนกลับไม่ได้ (คืนรถ) และข้อมูลส่วนตัวทั้งก้อน (Excel) สงวนไว้ให้ admin

### ตั้งรหัสผ่าน

```bash
python -m ocrslip.auth hash 'รหัสผ่านที่ต้องการ'    # ได้ scrypt hash มาใส่ .env
python -c "import secrets;print(secrets.token_urlsafe(32))"   # ได้ SECRET_KEY
```

`.env` เก็บแค่ **hash** ไม่เคยเก็บรหัสผ่านจริง

### กันเดารหัสผ่าน

- scrypt (`n=16384`) ช้าโดยตั้งใจ ทำให้เดารัว ๆ ไม่คุ้ม
- นับความพยายามที่ผิดแยกทั้งราย IP และรายชื่อผู้ใช้ — พลาดครบ 5 ครั้งใน 15 นาทีแล้วล็อก
  30 วินาที และเพิ่มเป็นเท่าตัวทุกครั้งที่พลาดซ้ำ
- เพดานการล็อกไม่เท่ากันโดยตั้งใจ: **ราย IP สูงสุด 30 นาที แต่รายชื่อผู้ใช้สูงสุดแค่ 3 นาที**
  เพราะใครก็ยิงชื่อ `admin` จาก IP ไหนก็ได้ ถ้าล็อกยาวเท่ากันจะกลายเป็นช่องให้กันเจ้าหน้าที่
  ตัวจริงเข้าระบบตอนฉุกเฉิน ซึ่งเสียหายกว่าการโดนเดารหัส
- ชื่อผู้ใช้ที่ไม่มีอยู่จริงก็ถูก verify กับ hash หลอก ใช้เวลาเท่ากัน จึงเดาไม่ได้ว่าบัญชีไหนมีจริง
- เทียบค่าด้วย `compare_digest` ทุกจุด, cookie เซ็นด้วย HMAC-SHA256, `HttpOnly` + `SameSite=Lax`
  (ตั้ง `COOKIE_SECURE=true` เมื่อรันหลัง HTTPS)

## ทดสอบ

```bash
.venv/bin/python -m pytest tests -q
```

## Deploy บน Render

ทั้ง `Dockerfile` และ `render.yaml` อยู่ใน repo แล้ว — Render จะ build image เองจาก Dockerfile
ไม่ต้องตั้ง build command / start command ใน dashboard

### ลองที่เครื่องก่อน

```bash
docker build -t ocr-dhammakaya .
docker run --rm -p 8000:8000 --env-file .env -e PORT=8000 ocr-dhammakaya
# เปิด http://localhost:8000
```

> **Mac ชิป Apple (arm64)**: wheel ของ `opencv-python-headless` 5.0.0.93 ฝั่ง linux/arm64
> crash ตอน `import cv2` (Illegal instruction) — ฝั่ง linux/amd64 ซึ่งเป็นสถาปัตยกรรมที่ Render ใช้
> ไม่มีปัญหา ถ้าจะลองที่เครื่อง Mac ให้ build เป็น amd64 ตรง ๆ (ช้ากว่าเพราะรันผ่าน emulator):
> ```bash
> docker build --platform linux/amd64 -t ocr-dhammakaya .
> docker run --rm --platform linux/amd64 -p 8000:8000 --env-file .env ocr-dhammakaya
> ```

image ไม่มีข้อมูลจริงติดไปด้วย — `.dockerignore` ตัด `example/`, `bench/out/`, `.env`, `.venv`, `tests/`
ออกหมด และ Dockerfile copy เฉพาะ `ocrslip/` (รวม template + static) กับ `db/schema.sql`
process รันด้วย user `ocrslip` (uid 10001) ไม่ใช่ root

### ขั้นตอนบน Render

1. เตรียม Postgres ก่อน — ใช้ **Render Postgres** หรือที่อื่น (Neon / Supabase) ก็ได้
   เอา connection string มาเก็บไว้ใช้เป็น `DATABASE_URL`
2. สร้าง hash ของรหัสผ่านไว้ล่วงหน้าที่เครื่องตัวเอง (ห้ามใส่รหัสผ่าน plaintext ลง env):
   ```bash
   .venv/bin/python -m ocrslip.auth hash 'รหัสผ่านของ admin'
   .venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"   # SECRET_KEY
   ```
3. Render Dashboard → **New → Blueprint** → เลือก repo นี้ → Render อ่าน `render.yaml` แล้วสร้าง
   web service ชื่อ `ocr-dhammakaya` ให้ (runtime docker, health check `/static/app.css`)
   (ถ้าอยากให้ health check ยิงหน้า login จริง ๆ เปลี่ยน `healthCheckPath` เป็น `/login` ได้ —
   ทั้งสองเส้นทางตอบ 200 โดยไม่ต้องต่อ DB)
4. Render จะถามค่า env ทุกตัวที่ประกาศเป็น `sync: false` — กรอกตามตารางข้างล่าง
   (`render.yaml` ไม่มีค่าลับอยู่เลย ค่าจริงอยู่ใน dashboard เท่านั้น)
5. กด deploy รอ build เสร็จ แล้วทำ **ข้อสำคัญ: สร้าง schema** ตามหัวข้อถัดไป
6. หลังจากนั้น push เข้า branch หลัก = deploy ใหม่อัตโนมัติ (`autoDeployTrigger: commit`)

### env ที่ต้องกรอกใน dashboard

| key | ตัวอย่าง / หมายเหตุ |
|---|---|
| `OPENROUTER_API_KEY` | `sk-or-v1-...` จาก https://openrouter.ai/keys |
| `DATABASE_URL` | `postgresql://user:pass@host/db` — ใช้ **Internal** URL ถ้า DB อยู่บน Render |
| `DB_SCHEMA` | `ocr_dhammakaya` |
| `OCR_MODEL` | `google/gemini-3-flash-preview` |
| `CONFIDENCE_THRESHOLD` | `0.85` |
| `USD_THB` | `33` |
| `SECRET_KEY` | ค่าที่สุ่มจากข้อ 2 — ถ้าไม่ตั้ง คนที่ล็อกอินอยู่จะหลุดทุกครั้งที่ deploy |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` | บัญชีแอดมิน (hash จากข้อ 2) |
| `USER_USERNAME` / `USER_PASSWORD_HASH` | บัญชีพนักงานหน้างาน |
| `COOKIE_SECURE` | `true` — Render เป็น HTTPS อยู่แล้ว |

Render ตั้ง `PORT` ให้เองตอน runtime, Dockerfile bind `0.0.0.0:$PORT` (default 8000 ถ้าไม่มี)
ไม่ต้องกรอก `PORT` เอง และ `.env` ไม่ถูก copy เข้า image — `config.py` อ่านจาก env ของ process ตรง ๆ

### สร้าง schema ครั้งแรก (ต้องทำ 1 ครั้ง)

`python -m ocrslip.db init` เป็น idempotent (รันซ้ำได้ ไม่พัง) แต่ **ไม่** ถูกใส่ไว้ใน CMD
เพราะไม่อยากให้ทุก restart ไปแตะ DDL และไม่อยากให้แอปบูตไม่ขึ้นตอน DB ล่ม

วิธีที่เลือกใช้: **รันมือเดียวผ่าน Render Shell** หลัง deploy แรกสำเร็จ
service → แท็บ **Shell** → พิมพ์:

```bash
python -m ocrslip.db init
```

จะได้ผลลัพธ์แบบ `schema ocr_dhammakaya พร้อมใช้งาน: ['slip_edits', 'slip_images', 'slips']`
จากนั้นรีเฟรชหน้าเว็บ — `/` ควรใช้งานได้แล้ว (ก่อน init หน้า `/` จะ 500 เพราะยังไม่มีตาราง)

> ถ้าใช้ plan ที่ไม่มี Shell ให้รันจากเครื่องตัวเองแทน โดยใช้ **External** `DATABASE_URL` ของ DB:
> ```bash
> DATABASE_URL='postgresql://...external...' .venv/bin/python -m ocrslip.db init
> ```
