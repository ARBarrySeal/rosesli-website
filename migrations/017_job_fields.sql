-- 017: Assignments page overhaul — split event address, room number, split
-- notes (admin-only vs interpreter-visible), and Deaf Consumer rows.
-- Idempotent: safe to re-run.
--
-- event_street/city/state mirror the Phase-3 profile address split; legacy
-- event_address stays as a read fallback. admin_notes renders ONLY for admins;
-- interpreter_notes is what interpreters see. The legacy notes column is left
-- untouched for the dod tenant.

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS event_street      TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS event_city        TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS event_state       TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS room_number       TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS admin_notes       TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS interpreter_notes TEXT;

CREATE TABLE IF NOT EXISTS job_consumers (
    id         SERIAL PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    name       TEXT,
    email      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_job_consumers_job ON job_consumers(job_id);

-- Backfill existing rosesli notes into the interpreter-visible field so
-- nothing disappears when the templates switch over (dod keeps notes as-is).
UPDATE jobs SET interpreter_notes = notes
WHERE company = 'rosesli' AND interpreter_notes IS NULL AND notes IS NOT NULL;
