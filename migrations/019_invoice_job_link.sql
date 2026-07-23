-- 019: Phase 1 — link an invoice to the specific job it bills, and add an
-- informational Expenses section. job_id is ON DELETE SET NULL so deleting an
-- assignment never drops a historical invoice. The partial unique index
-- mirrors migrations/012_invoice_jobs_unique_job.sql: a job can be billed by
-- at most one individual invoice, so re-creating an invoice for the same job
-- (double-click, two tabs) is rejected at the DB level.
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS expenses TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_job_id
    ON invoices(job_id) WHERE job_id IS NOT NULL;
