-- 020: Phase 2 (2026-07-22 batch) — Client Review. Client invoices gain a
-- submitted flag mirroring invoices.submitted: new invoices are created as
-- drafts (submitted=FALSE) that only admins can see, until an admin reviews
-- and submits them via the new Client Review page — at which point they
-- become visible to the client. DEFAULT TRUE backfills every existing row so
-- invoices clients have already seen don't disappear from their view.
ALTER TABLE client_invoices ADD COLUMN IF NOT EXISTS submitted BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE client_invoices ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP;
