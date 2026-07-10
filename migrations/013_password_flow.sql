-- 013: Forced-password-change flag for the new password lifecycle.
-- Idempotent: safe to re-run.
--
-- Set TRUE when (a) an account is created with the default password,
-- (b) an admin issues a temporary password, or (c) forgot-password emails a
-- temporary password (rosesli). While TRUE, login_required redirects every
-- portal request to /portal/change-password until the user sets a real one.

ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
