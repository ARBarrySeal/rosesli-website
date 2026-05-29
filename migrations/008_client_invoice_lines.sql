-- 008: Add line_items column to client_invoices for multi-line differential support.
-- Idempotent: safe to re-run.

ALTER TABLE client_invoices ADD COLUMN IF NOT EXISTS line_items TEXT;
