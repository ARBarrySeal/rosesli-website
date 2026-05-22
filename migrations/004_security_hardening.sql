-- Migration 004: security hardening — account lockout, MFA enrollment, audit log,
-- and password-rotation JWT invalidation. Idempotent (safe to re-run).

-- Account lockout: brute-force resistance via attempt counter + temporary lock.
ALTER TABLE portal_users
    ADD COLUMN IF NOT EXISTS failed_login_count   INT          NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS locked_until         TIMESTAMPTZ,
    -- Tokens issued before pw_changed_at are rejected by login_required.
    ADD COLUMN IF NOT EXISTS pw_changed_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    -- TOTP (RFC 6238). Required for admins; opt-in for everyone else.
    ADD COLUMN IF NOT EXISTS mfa_secret           TEXT,
    ADD COLUMN IF NOT EXISTS mfa_enrolled_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS mfa_recovery_hashes  JSONB        NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_portal_users_locked_until
    ON portal_users(locked_until) WHERE locked_until IS NOT NULL;

-- Audit log. Append-only from app code (no UPDATE/DELETE statements anywhere).
CREATE TABLE IF NOT EXISTS portal_audit (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id    INT REFERENCES portal_users(id) ON DELETE SET NULL,
    -- Email captured at write-time so the audit row survives user deletion.
    email      TEXT,
    company    TEXT,
    action     TEXT NOT NULL,
    target     TEXT,
    ip         INET,
    ua         TEXT,
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_portal_audit_user_id ON portal_audit(user_id);
CREATE INDEX IF NOT EXISTS idx_portal_audit_ts      ON portal_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_portal_audit_action  ON portal_audit(action);
CREATE INDEX IF NOT EXISTS idx_portal_audit_company ON portal_audit(company);
