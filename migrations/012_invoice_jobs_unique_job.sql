-- 012: Phase 6 hardening — an assignment can be billed on at most one invoice.
-- The application already filters out already-invoiced jobs, but that check is a
-- read: two near-simultaneous submits (double-click / two tabs) could both pass
-- it and bill the same assignment twice. This unique index makes the database
-- itself reject the duplicate, so the second submit rolls back cleanly.
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_jobs_job
    ON invoice_jobs(job_id) WHERE job_id IS NOT NULL;
