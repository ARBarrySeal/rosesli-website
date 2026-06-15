-- 010: Interpreter specialty for offers-by-category matching.
-- Idempotent: safe to re-run.
--
-- Stores the assignment category/categories an interpreter specializes in
-- (e.g. "Legal", "Medical"). The coordinator's offer surface sorts and flags
-- matching interpreters first for a job of that assignment_type.

ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS specialty TEXT;
