CREATE TABLE portal_documents (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    company       TEXT    NOT NULL,
    filename      TEXT    NOT NULL,
    original_name TEXT    NOT NULL,
    mime_type     TEXT,
    size_bytes    BIGINT,
    uploaded_by   INTEGER REFERENCES portal_users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_portal_documents_user_id ON portal_documents(user_id);
CREATE INDEX idx_portal_documents_company ON portal_documents(company);
