-- Schema สำหรับระบบ OCR ใบฝากรถน้ำท่วม
-- ปลอดภัยต่อการรันซ้ำ (idempotent) และแยก schema ของตัวเองไม่แตะของเดิมในฐานข้อมูลเดียวกัน

CREATE SCHEMA IF NOT EXISTS ocr_dhammakaya;

CREATE EXTENSION IF NOT EXISTS pg_trgm;        -- fuzzy search ชื่อ/ทะเบียน
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;  -- levenshtein สำหรับเบอร์โทร/ทะเบียน
CREATE EXTENSION IF NOT EXISTS pgcrypto;       -- gen_random_uuid()

-- ใบฝากรถ 1 แถว = 1 ใบ
CREATE TABLE IF NOT EXISTS ocr_dhammakaya.slips (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- ข้อมูลที่คนยืนยันแล้ว (ค่าที่ใช้งานจริง)
    name            text,
    name_norm       text,
    tel             text,
    tel_digits      text,
    plate_raw       text,
    plate_norm      text,
    province        text,
    brand           text,
    brand_norm      text,
    car_type        text,
    location        text,          -- ที่จอด เช่น "อาคาร 3 ชั้น 5 c2"
    deposit_date    date,
    note            text,

    -- สถานะการตรวจสอบข้อมูล (คนละเรื่องกับสถานะรถ)
    review_status   text NOT NULL DEFAULT 'pending'
                    CHECK (review_status IN ('pending', 'approved', 'rejected')),
    needs_review    boolean NOT NULL DEFAULT true,
    review_reason   text[] NOT NULL DEFAULT '{}',
    reviewed_by     text,
    reviewed_at     timestamptz,

    -- สถานะรถ
    car_status      text NOT NULL DEFAULT 'stored'
                    CHECK (car_status IN ('stored', 'returned')),
    returned_at     timestamptz,
    returned_by     text,          -- ชื่อเจ้าหน้าที่ที่คืนรถ
    returned_note   text,

    -- ร่องรอยของ OCR ไว้ตรวจย้อนหลัง
    ocr_model       text,
    ocr_variant     text,
    ocr_confidence  jsonb NOT NULL DEFAULT '{}'::jsonb,
    ocr_cost_usd    numeric(12, 8) NOT NULL DEFAULT 0,   -- ค่าใช้จ่ายจริงที่ OpenRouter คิด
    ocr_tokens_in   integer NOT NULL DEFAULT 0,
    ocr_tokens_out  integer NOT NULL DEFAULT 0,
    ocr_latency_s   numeric(8, 2) NOT NULL DEFAULT 0,
    raw_ocr         jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- created_by = บัญชีที่ล็อกอิน (staff/admin) ปลอมไม่ได้ ใช้เป็น audit trail
    created_by      text,
    -- uploaded_by / photographer = ชื่อคนจริงที่เลือกตอนอัปโหลด
    -- เพราะ staff ใช้บัญชีร่วมกันหลายคน ถ้าดูแค่ created_by จะเป็น "staff" เหมือนกันหมด
    uploaded_by     text,
    photographer    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- รูปหลักฐาน เก็บไว้ในฐานข้อมูลเพื่อให้หลักฐานติดอยู่กับเรคอร์ดเสมอ
CREATE TABLE IF NOT EXISTS ocr_dhammakaya.slip_images (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slip_id    uuid NOT NULL REFERENCES ocr_dhammakaya.slips(id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('original', 'processed')),
    mime       text NOT NULL DEFAULT 'image/jpeg',
    sha256     text NOT NULL,
    width      integer,
    height     integer,
    bytes      bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- รายชื่อเจ้าหน้าที่/อาสาสมัคร สำหรับให้เลือกตอนอัปโหลด
-- แยกจากบัญชีล็อกอิน เพราะ staff ใช้บัญชีร่วมกันแต่เป็นคนละคน
CREATE TABLE IF NOT EXISTS ocr_dhammakaya.staff_members (
    id         bigserial PRIMARY KEY,
    name       text NOT NULL,
    active     boolean NOT NULL DEFAULT true,
    note       text,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS staff_members_name_key
    ON ocr_dhammakaya.staff_members (lower(btrim(name)));

-- ทุกการแก้ค่าโดยคน เก็บไว้ทั้งหมด (ใช้ดูว่า model พลาด field ไหนบ่อย)
CREATE TABLE IF NOT EXISTS ocr_dhammakaya.slip_edits (
    id         bigserial PRIMARY KEY,
    slip_id    uuid NOT NULL REFERENCES ocr_dhammakaya.slips(id) ON DELETE CASCADE,
    field      text NOT NULL,
    old_value  text,
    new_value  text,
    edited_by  text,
    edited_at  timestamptz NOT NULL DEFAULT now()
);

-- unique ต่อ "ใบ" ไม่ใช่ต่อทั้งตาราง — ทุกใบต้องมีรูปหลักฐานของตัวเองเสมอ
-- ส่วนรูปที่ hash ซ้ำกับใบอื่น จะถูกชู flag duplicate_image ในคิวตรวจแทนการทิ้งรูป
DROP INDEX IF EXISTS ocr_dhammakaya.slip_images_sha256_key;
CREATE UNIQUE INDEX IF NOT EXISTS slip_images_slip_sha_key
    ON ocr_dhammakaya.slip_images (slip_id, sha256);
CREATE INDEX IF NOT EXISTS slip_images_sha256_idx
    ON ocr_dhammakaya.slip_images (sha256);
CREATE INDEX IF NOT EXISTS slip_images_slip_id_idx
    ON ocr_dhammakaya.slip_images (slip_id, kind);

-- index สำหรับ fuzzy search
CREATE INDEX IF NOT EXISTS slips_name_trgm  ON ocr_dhammakaya.slips USING gin (name_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS slips_plate_trgm ON ocr_dhammakaya.slips USING gin (plate_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS slips_brand_trgm ON ocr_dhammakaya.slips USING gin (brand_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS slips_tel_idx    ON ocr_dhammakaya.slips (tel_digits);
CREATE INDEX IF NOT EXISTS slips_car_idx    ON ocr_dhammakaya.slips (car_status, deposit_date);

-- คิวตรวจกรองด้วย (review_status, needs_review) แล้วเรียง created_at เสมอ
-- ถ้า created_at ไม่อยู่ใน index Postgres ต้องอ่านทั้งกองออกมา sort ใหม่ทุกครั้งที่เปิดหน้า
-- index ตัวนี้ครอบ slips_review_idx เดิมทั้งหมด (prefix เดียวกัน) จึงทิ้งตัวเก่าไป
DROP INDEX IF EXISTS ocr_dhammakaya.slips_review_idx;
CREATE INDEX IF NOT EXISTS slips_review_recent_idx
    ON ocr_dhammakaya.slips (review_status, needs_review, created_at DESC);

-- ช่องค้นหาใช้ ILIKE '%คำ%' ซึ่งมี wildcard นำหน้า btree ช่วยไม่ได้ ต้องเป็น trigram
-- ที่จอดกับเบอร์โทรยังไม่มี index รองรับ ทำให้ทุกการค้นกวาดทั้งตาราง
CREATE INDEX IF NOT EXISTS slips_location_trgm ON ocr_dhammakaya.slips USING gin (location gin_trgm_ops);
CREATE INDEX IF NOT EXISTS slips_tel_trgm      ON ocr_dhammakaya.slips USING gin (tel_digits gin_trgm_ops);

CREATE INDEX IF NOT EXISTS slip_edits_slip_idx ON ocr_dhammakaya.slip_edits (slip_id, edited_at DESC);

-- อัปเดต updated_at อัตโนมัติ
CREATE OR REPLACE FUNCTION ocr_dhammakaya.touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS slips_touch_updated_at ON ocr_dhammakaya.slips;
CREATE TRIGGER slips_touch_updated_at
    BEFORE UPDATE ON ocr_dhammakaya.slips
    FOR EACH ROW EXECUTE FUNCTION ocr_dhammakaya.touch_updated_at();

-- คอลัมน์ที่เพิ่มทีหลัง (ปลอดภัยกับฐานข้อมูลที่สร้างไปแล้ว)
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS ocr_cost_usd   numeric(12, 8) NOT NULL DEFAULT 0;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS ocr_tokens_in  integer NOT NULL DEFAULT 0;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS ocr_tokens_out integer NOT NULL DEFAULT 0;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS ocr_latency_s  numeric(8, 2) NOT NULL DEFAULT 0;

ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS uploaded_by  text;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS photographer text;
CREATE INDEX IF NOT EXISTS slips_uploaded_by_idx  ON ocr_dhammakaya.slips (uploaded_by);
CREATE INDEX IF NOT EXISTS slips_photographer_idx ON ocr_dhammakaya.slips (photographer);
