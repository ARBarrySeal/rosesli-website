-- 005: USked-style scheduler — jobs/assignments, profile fields, invoice fields, attachments.
-- Idempotent: safe to re-run.

-- ── Profile fields ────────────────────────────────────────────────────────────
-- Interpreter (employee) fields
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS zip              TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS certification    TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS interpreter_rate NUMERIC(10,2);
-- Client billing fields
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS point_of_contact TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS billing_email    TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS billing_phone    TEXT;

-- ── Jobs / assignments ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    id                 SERIAL PRIMARY KEY,
    company            TEXT NOT NULL CHECK (company IN ('dod', 'rosesli')),
    status             TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'confirmed', 'completed', 'cancelled')),
    source             TEXT NOT NULL DEFAULT 'manual'
                            CHECK (source IN ('manual', 'public_request')),

    -- requester (captured from the public request form)
    requester_name     TEXT,
    requester_email    TEXT,
    requester_phone    TEXT,
    organization       TEXT,

    -- client / billing
    client_id          INT REFERENCES portal_users(id) ON DELETE SET NULL,
    client_name        TEXT,
    client_address     TEXT,

    -- event
    event_address      TEXT,
    event_zip          TEXT,
    setting            TEXT,
    service_format     TEXT,
    dress_code         TEXT,
    deaf_clients       TEXT,

    -- on-site point of contact
    poc_name           TEXT,
    poc_email          TEXT,
    poc_phone          TEXT,

    -- schedule
    event_date         DATE,
    start_time         TEXT,
    end_time           TEXT,
    duration           TEXT,

    -- interpreters (FK drives interpreter visibility; *_name is a display snapshot)
    num_interpreters   INT,
    interpreter_1_id   INT REFERENCES portal_users(id) ON DELETE SET NULL,
    interpreter_2_id   INT REFERENCES portal_users(id) ON DELETE SET NULL,
    interpreter_1_name TEXT,
    interpreter_2_name TEXT,

    -- money
    client_rate        NUMERIC(10,2),
    rate_type          TEXT DEFAULT 'hourly' CHECK (rate_type IN ('hourly', 'flat')),

    notes              TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_date    ON jobs(event_date);
CREATE INDEX IF NOT EXISTS idx_jobs_interp1 ON jobs(interpreter_1_id) WHERE interpreter_1_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_interp2 ON jobs(interpreter_2_id) WHERE interpreter_2_id IS NOT NULL;

-- ── Invoice scheduler fields ──────────────────────────────────────────────────
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS job_id            INT REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS interpreter_rates TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS notes             TEXT;

-- ── Invoice attachments (mirrors portal_documents) ────────────────────────────
CREATE TABLE IF NOT EXISTS invoice_attachments (
    id            SERIAL PRIMARY KEY,
    invoice_id    INT  NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    company       TEXT NOT NULL,
    filename      TEXT NOT NULL,
    original_name TEXT NOT NULL,
    mime_type     TEXT,
    size_bytes    BIGINT,
    uploaded_by   INT REFERENCES portal_users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_invoice_attachments_invoice ON invoice_attachments(invoice_id);
