-- 016: Multi-interpreter assignments (join table) + "number required".
-- Idempotent: safe to re-run.
--
-- job_interpreters is the source of truth for who is staffed on a job; slots
-- 1-2 are WRITE-THROUGH MIRRORED into the legacy jobs.interpreter_1_*/2_*
-- columns on every write so untouched read paths (dashboards, invoice pickers,
-- dod tenant) keep working. Legacy columns are retired in a future cleanup.
-- num_required = how many interpreters must personally confirm before the job
-- flips to 'confirmed' (falls back to num_interpreters, then 1).

CREATE TABLE IF NOT EXISTS job_interpreters (
    id               SERIAL PRIMARY KEY,
    job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    interpreter_id   INTEGER NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    interpreter_name TEXT,
    slot             INTEGER NOT NULL DEFAULT 1,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (job_id, interpreter_id)
);
CREATE INDEX IF NOT EXISTS idx_job_interpreters_interp ON job_interpreters(interpreter_id);
CREATE INDEX IF NOT EXISTS idx_job_interpreters_job ON job_interpreters(job_id, slot);

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS num_required INTEGER;

-- Backfill from the legacy slot columns.
INSERT INTO job_interpreters (job_id, interpreter_id, interpreter_name, slot)
SELECT id, interpreter_1_id, interpreter_1_name, 1
FROM jobs WHERE interpreter_1_id IS NOT NULL
ON CONFLICT (job_id, interpreter_id) DO NOTHING;

INSERT INTO job_interpreters (job_id, interpreter_id, interpreter_name, slot)
SELECT id, interpreter_2_id, interpreter_2_name, 2
FROM jobs WHERE interpreter_2_id IS NOT NULL
ON CONFLICT (job_id, interpreter_id) DO NOTHING;
