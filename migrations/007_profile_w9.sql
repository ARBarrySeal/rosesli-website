-- 007: W-9 attachments per interpreter profile.
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS profile_w9 (
    id            SERIAL PRIMARY KEY,
    user_id       INT NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    company       TEXT NOT NULL,
    year          INT NOT NULL,
    gcs_key       TEXT NOT NULL,
    original_name TEXT NOT NULL,
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_profile_w9_user ON profile_w9(user_id);
