-- 006: multi-update — client invoices, interpreter invoice fields, job enhancements,
--       archived users, profile type.
-- Idempotent: safe to re-run.

-- ── Interpreter invoice fields ────────────────────────────────────────────────
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS date_of_service   DATE;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS service_start_time TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS service_end_time   TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS duration_hours     NUMERIC(5,2);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS base_rate          NUMERIC(10,2);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS differential       TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS rate_applied       NUMERIC(10,2);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS cancellation_type  TEXT
    CHECK (cancellation_type IN ('xcl_lt48', 'xcl') OR cancellation_type IS NULL);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS submitted          BOOLEAN DEFAULT FALSE;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS submitted_at       TIMESTAMPTZ;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS paid_date          DATE;

-- ── Client invoices ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS client_invoices (
    id               SERIAL PRIMARY KEY,
    company          TEXT NOT NULL,
    client_id        INT REFERENCES portal_users(id) ON DELETE SET NULL,
    client_name      TEXT,
    poc_email        TEXT,
    poc_phone        TEXT,
    date_of_service  DATE,
    start_time       TEXT,
    end_time         TEXT,
    duration_hours   NUMERIC(5,2),
    rate_per_hour    NUMERIC(10,2),
    incidentals      NUMERIC(10,2) DEFAULT 0,
    total            NUMERIC(10,2),
    status           TEXT NOT NULL DEFAULT 'unpaid' CHECK (status IN ('paid', 'unpaid')),
    paid_date        DATE,
    notes            TEXT,
    job_id           INT REFERENCES jobs(id) ON DELETE SET NULL,
    created_by       INT REFERENCES portal_users(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_client_invoices_company ON client_invoices(company);
CREATE INDEX IF NOT EXISTS idx_client_invoices_client  ON client_invoices(client_id);
CREATE INDEX IF NOT EXISTS idx_client_invoices_status  ON client_invoices(status);

-- ── Job enhancements ──────────────────────────────────────────────────────────
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS job_number      INT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS assignment_type TEXT
    CHECK (assignment_type IN (
        'Conference','Educational','Legal','Medical',
        'Other','Performance/Entertainment','Private Event','Work Related'
    ) OR assignment_type IS NULL);

-- Back-fill sequential job numbers per company for existing rows
DO $$
BEGIN
    WITH numbered AS (
        SELECT id,
               ROW_NUMBER() OVER (PARTITION BY company ORDER BY created_at, id) AS n
        FROM jobs
        WHERE job_number IS NULL
    )
    UPDATE jobs SET job_number = numbered.n
    FROM numbered
    WHERE jobs.id = numbered.id;
END $$;

-- ── Archived users ────────────────────────────────────────────────────────────
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS archived    BOOLEAN DEFAULT FALSE;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
