-- 011: Phase 6 — interpreter master invoice.
-- Interpreters review their confirmed/completed assignments, select which to
-- bill, and bundle them into ONE master invoice that is submitted for payment.

-- ── New 'submitted_for_payment' status on interpreter invoices ────────────────
ALTER TABLE invoices DROP CONSTRAINT IF EXISTS invoices_status_check;
ALTER TABLE invoices ADD CONSTRAINT invoices_status_check
    CHECK (status IN ('unpaid', 'paid', 'overdue', 'submitted_for_payment'));

-- ── Assignment lines covered by a master invoice ──────────────────────────────
-- One invoices row (the master) fans out to many jobs via this join table.
-- job_id is ON DELETE SET NULL so deleting an assignment never drops the
-- invoice line's recorded amount.
CREATE TABLE IF NOT EXISTS invoice_jobs (
    id          SERIAL PRIMARY KEY,
    invoice_id  INT  NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    job_id      INT  REFERENCES jobs(id) ON DELETE SET NULL,
    company     TEXT NOT NULL,
    line_amount NUMERIC(10,2),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_invoice_jobs_invoice ON invoice_jobs(invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoice_jobs_job     ON invoice_jobs(job_id);
