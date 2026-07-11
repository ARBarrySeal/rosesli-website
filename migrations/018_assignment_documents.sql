-- 018: Assignment documents — job-scoped rows in the existing portal_documents
-- store. job_id IS NULL = user/profile document (all pre-existing rows);
-- job_id set = attached to an assignment, served only via the job-scoped
-- routes (admin + staffed interpreters).
-- Idempotent: safe to re-run.

ALTER TABLE portal_documents
    ADD COLUMN IF NOT EXISTS job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_portal_documents_job ON portal_documents (job_id);
