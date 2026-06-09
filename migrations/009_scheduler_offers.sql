-- 009: Interpreter availability blocks + job offers (Usked-style matching).
-- Idempotent: safe to re-run.

-- ── Availability: block-off-unavailable model ────────────────────────────────
-- Interpreters mark when they CANNOT work. Absence of a block = available.
CREATE TABLE IF NOT EXISTS interpreter_unavailability (
    id          SERIAL PRIMARY KEY,
    company     TEXT NOT NULL CHECK (company IN ('dod','rosesli')),
    user_id     INT  NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,              -- = start_date for a single day
    all_day     BOOLEAN NOT NULL DEFAULT TRUE,
    start_time  TEXT,                       -- only when all_day = FALSE
    end_time    TEXT,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_unavail_user  ON interpreter_unavailability(user_id);
CREATE INDEX IF NOT EXISTS idx_unavail_dates ON interpreter_unavailability(start_date, end_date);

-- ── Job offers: coordinator-offers matching ──────────────────────────────────
-- Amanda offers a job to one or more interpreters; they accept/decline.
CREATE TABLE IF NOT EXISTS job_offers (
    id             SERIAL PRIMARY KEY,
    company        TEXT NOT NULL CHECK (company IN ('dod','rosesli')),
    job_id         INT  NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    interpreter_id INT  NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT 'offered'
                        CHECK (status IN ('offered','accepted','declined','withdrawn')),
    note           TEXT,
    offered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    responded_at   TIMESTAMPTZ,
    UNIQUE (job_id, interpreter_id)
);
CREATE INDEX IF NOT EXISTS idx_offers_job    ON job_offers(job_id);
CREATE INDEX IF NOT EXISTS idx_offers_interp ON job_offers(interpreter_id);
CREATE INDEX IF NOT EXISTS idx_offers_status ON job_offers(status);
