-- 021: Phase 5 (2026-07-22 batch) — Billing section. Wires the differential
-- into the client invoice's MAIN line (resolves the 2026-06-20 batch's open
-- #15: previously a differential only added on top via an extra line) and
-- adds a flat-rate mode. Mirrors invoices.base_rate/differential exactly —
-- rate_per_hour keeps its existing meaning (the effective rate this invoice
-- bills at = base_rate + differential), so every existing reader of
-- rate_per_hour (list/detail templates, portal_rates recalculation) keeps
-- working unchanged.
ALTER TABLE client_invoices ADD COLUMN IF NOT EXISTS rate_type TEXT NOT NULL DEFAULT 'hourly' CHECK (rate_type IN ('hourly', 'flat'));
ALTER TABLE client_invoices ADD COLUMN IF NOT EXISTS base_rate NUMERIC(10,2);
ALTER TABLE client_invoices ADD COLUMN IF NOT EXISTS differential TEXT;
