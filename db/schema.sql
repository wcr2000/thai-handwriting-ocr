-- Schema for the flood parking-slip OCR system.
-- Safe to re-run (idempotent), and confined to its own schema so it never touches anything
-- else sharing the database.

CREATE SCHEMA IF NOT EXISTS ocr_dhammakaya;

CREATE EXTENSION IF NOT EXISTS pg_trgm;        -- fuzzy search over names and plates
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;  -- levenshtein for phone numbers and plates
CREATE EXTENSION IF NOT EXISTS pgcrypto;       -- gen_random_uuid()

-- One row = one parking slip
CREATE TABLE IF NOT EXISTS ocr_dhammakaya.slips (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Human-confirmed data (the values actually in use)
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
    location        text,          -- parking spot, e.g. "อาคาร 3 ชั้น 5 c2" (building 3, floor 5, bay c2)
    deposit_date    date,
    note            text,

    -- Data review status (a separate matter from the car's status)
    review_status   text NOT NULL DEFAULT 'pending'
                    CHECK (review_status IN ('pending', 'approved', 'rejected')),
    needs_review    boolean NOT NULL DEFAULT true,
    review_reason   text[] NOT NULL DEFAULT '{}',
    reviewed_by     text,
    reviewed_at     timestamptz,

    -- Car status
    car_status      text NOT NULL DEFAULT 'stored'
                    CHECK (car_status IN ('stored', 'returned')),
    returned_at     timestamptz,
    returned_by     text,          -- name of the staff member who released the car
    returned_note   text,

    -- The OCR audit trail, kept for later inspection
    ocr_model       text,
    ocr_variant     text,
    ocr_confidence  jsonb NOT NULL DEFAULT '{}'::jsonb,
    ocr_cost_usd    numeric(12, 8) NOT NULL DEFAULT 0,   -- what OpenRouter actually billed
    ocr_tokens_in   integer NOT NULL DEFAULT 0,
    ocr_tokens_out  integer NOT NULL DEFAULT 0,
    ocr_latency_s   numeric(8, 2) NOT NULL DEFAULT 0,
    raw_ocr         jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- created_by = the logged-in account (staff/admin). Unforgeable, so it serves as an audit trail.
    created_by      text,
    -- uploaded_by / photographer = the real person's name chosen at upload time, because the
    -- staff account is shared by several people and created_by would read "staff" for all of them.
    uploaded_by     text,
    photographer    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Evidence images, stored in the database so the evidence always travels with the record
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

-- The roster of staff and volunteers offered as choices at upload time.
-- Kept separate from login accounts, because the staff account is shared by distinct people.
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

-- Every human edit, retained in full (used to see which fields the model gets wrong most often)
CREATE TABLE IF NOT EXISTS ocr_dhammakaya.slip_edits (
    id         bigserial PRIMARY KEY,
    slip_id    uuid NOT NULL REFERENCES ocr_dhammakaya.slips(id) ON DELETE CASCADE,
    field      text NOT NULL,
    old_value  text,
    new_value  text,
    edited_by  text,
    edited_at  timestamptz NOT NULL DEFAULT now()
);

-- Unique per *slip*, not across the whole table: every slip must always have its own evidence
-- image. An image whose hash matches another slip is flagged duplicate_image in the review queue
-- rather than discarded.
DROP INDEX IF EXISTS ocr_dhammakaya.slip_images_sha256_key;
CREATE UNIQUE INDEX IF NOT EXISTS slip_images_slip_sha_key
    ON ocr_dhammakaya.slip_images (slip_id, sha256);
CREATE INDEX IF NOT EXISTS slip_images_sha256_idx
    ON ocr_dhammakaya.slip_images (sha256);
CREATE INDEX IF NOT EXISTS slip_images_slip_id_idx
    ON ocr_dhammakaya.slip_images (slip_id, kind);

-- Indexes for fuzzy search
CREATE INDEX IF NOT EXISTS slips_name_trgm  ON ocr_dhammakaya.slips USING gin (name_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS slips_plate_trgm ON ocr_dhammakaya.slips USING gin (plate_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS slips_brand_trgm ON ocr_dhammakaya.slips USING gin (brand_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS slips_tel_idx    ON ocr_dhammakaya.slips (tel_digits);
CREATE INDEX IF NOT EXISTS slips_car_idx    ON ocr_dhammakaya.slips (car_status, deposit_date);

-- The review queue always filters on (review_status, needs_review) and then orders by created_at.
-- With created_at outside the index, Postgres has to read the whole pile and re-sort it on every
-- page load. This index subsumes the old slips_review_idx entirely (same prefix), so that one is
-- dropped.
DROP INDEX IF EXISTS ocr_dhammakaya.slips_review_idx;
CREATE INDEX IF NOT EXISTS slips_review_recent_idx
    ON ocr_dhammakaya.slips (review_status, needs_review, created_at DESC);

-- The search box uses ILIKE '%term%'. A leading wildcard rules out btree, so these have to be
-- trigram indexes. Parking spot and phone number previously had no index at all, which made
-- every search a full table scan.
CREATE INDEX IF NOT EXISTS slips_location_trgm ON ocr_dhammakaya.slips USING gin (location gin_trgm_ops);
CREATE INDEX IF NOT EXISTS slips_tel_trgm      ON ocr_dhammakaya.slips USING gin (tel_digits gin_trgm_ops);

CREATE INDEX IF NOT EXISTS slip_edits_slip_idx ON ocr_dhammakaya.slip_edits (slip_id, edited_at DESC);

-- Keep updated_at current automatically
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

-- Columns added later (safe against an already-created database)
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS ocr_cost_usd   numeric(12, 8) NOT NULL DEFAULT 0;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS ocr_tokens_in  integer NOT NULL DEFAULT 0;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS ocr_tokens_out integer NOT NULL DEFAULT 0;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS ocr_latency_s  numeric(8, 2) NOT NULL DEFAULT 0;

-- Claiming slips in the review queue. Advisory only: what guarantees data is not overwritten is
-- the review_status condition inside the approval UPDATE, not these columns.
-- claimed_by is a worker id from a cookie rather than an account name, because three people
-- sharing one account cannot otherwise be told apart.
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS claimed_by   text;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS claimed_name text;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS claimed_at   timestamptz;

-- A slip that is a duplicate of one already reviewed (one paper slip uploaded twice).
-- It points at whichever slip is canonical and withdraws itself from the queue. Nothing is
-- deleted, because the evidence image and the record of who uploaded it when still matter for a
-- later audit — and an admin can delete it afterwards anyway.
-- ON DELETE SET NULL: if the canonical slip is deleted, the duplicate must return to the queue
-- rather than both vanishing.
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS superseded_by uuid
    REFERENCES ocr_dhammakaya.slips(id) ON DELETE SET NULL;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS superseded_at timestamptz;
CREATE INDEX IF NOT EXISTS slips_superseded_idx
    ON ocr_dhammakaya.slips (superseded_by) WHERE superseded_by IS NOT NULL;

-- The queue reads only the pending pile, a minority of the table, so a partial index is both
-- small and exactly on point. Its ordering matches the claim statement's ORDER BY, so the head of
-- the queue can be taken without a re-sort. superseded_by has to be in the predicate too, because
-- the claim statement filters on that same condition.
CREATE INDEX IF NOT EXISTS slips_claimable_idx
    ON ocr_dhammakaya.slips (needs_review DESC, created_at)
    WHERE review_status = 'pending' AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS slips_claimed_by_idx
    ON ocr_dhammakaya.slips (claimed_by) WHERE claimed_by IS NOT NULL;

ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS uploaded_by  text;
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS photographer text;
CREATE INDEX IF NOT EXISTS slips_uploaded_by_idx  ON ocr_dhammakaya.slips (uploaded_by);
CREATE INDEX IF NOT EXISTS slips_photographer_idx ON ocr_dhammakaya.slips (photographer);

-- Self-service slips (paperless entry) have to be distinguishable from slips that came from a
-- photographed hand-written slip, because self-service slips have no evidence image and never
-- pass through the review queue.
-- NULL means an older slip, all of which came from OCR.
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS entry_source text;

-- Name of the person collecting the car, recorded only when it is not the owner (a relative
-- collecting on their behalf). The case most in need of a record, yet it used to be capturable
-- only in the free-text note.
ALTER TABLE ocr_dhammakaya.slips ADD COLUMN IF NOT EXISTS released_to text;

-- Settings an admin can change from the web UI without touching env and restarting the service.
-- Stored as plain key/value, because there are only a handful of them and they are all text.
-- Modelling them as columns would mean a migration for every new setting, which is not worth it
-- for a table holding under ten rows.
-- No row means it has never been set from the web UI, so the env value always takes over.
CREATE TABLE IF NOT EXISTS ocr_dhammakaya.app_settings (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- A visit to a car that stays parked: the owner came to start it, check on it, or take something
-- out of it. Kept apart from slips because the slip's own state does not change (the car is
-- still here), yet the visit still has to be on record, attested by staff at the screening
-- point, the same way the way in and the way out are.
-- slip_id is the parked slip the plate and phone number matched at the time of the visit.
-- ON DELETE CASCADE: a visit to a slip that was deleted as junk is not worth keeping on its own.
CREATE TABLE IF NOT EXISTS ocr_dhammakaya.car_checks (
    id         bigserial PRIMARY KEY,
    slip_id    uuid NOT NULL REFERENCES ocr_dhammakaya.slips(id) ON DELETE CASCADE,
    tel        text,
    plate_raw  text,
    reason     text NOT NULL,
    checked_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS car_checks_slip_idx
    ON ocr_dhammakaya.car_checks (slip_id, created_at DESC);

-- The other half of a visit: when the owner comes back past the screening point having finished
-- with the car. NULL means they are still at the car (or never reported back), which is exactly
-- the list staff need when asking "who is in the car park right now".
ALTER TABLE ocr_dhammakaya.car_checks ADD COLUMN IF NOT EXISTS finished_at timestamptz;
ALTER TABLE ocr_dhammakaya.car_checks ADD COLUMN IF NOT EXISTS finished_by text;
CREATE INDEX IF NOT EXISTS car_checks_open_idx
    ON ocr_dhammakaya.car_checks (slip_id) WHERE finished_at IS NULL;
