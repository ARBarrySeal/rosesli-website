CREATE TABLE IF NOT EXISTS portal_users (
    id               SERIAL PRIMARY KEY,
    email            TEXT UNIQUE NOT NULL,
    password_hash    TEXT,
    full_name        TEXT,
    phone            TEXT,
    company_name     TEXT,
    address          TEXT,
    role             TEXT NOT NULL CHECK (role IN ('admin', 'employee', 'client')),
    company          TEXT NOT NULL CHECK (company IN ('dod', 'rosesli')),
    invite_token     TEXT,
    invite_expires   TIMESTAMPTZ,
    reset_token      TEXT,
    reset_expires    TIMESTAMPTZ,
    active           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_portal_users_email        ON portal_users(email);
CREATE INDEX IF NOT EXISTS idx_portal_users_company      ON portal_users(company);
CREATE INDEX IF NOT EXISTS idx_portal_users_invite_token ON portal_users(invite_token) WHERE invite_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_portal_users_reset_token  ON portal_users(reset_token)  WHERE reset_token  IS NOT NULL;

CREATE TABLE IF NOT EXISTS invoices (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    amount      NUMERIC(10,2) NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'unpaid' CHECK (status IN ('unpaid', 'paid', 'overdue')),
    due_date    DATE,
    pdf_url     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_invoices_user_id ON invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status  ON invoices(status);

CREATE TABLE IF NOT EXISTS payments (
    id           SERIAL PRIMARY KEY,
    invoice_id   INT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    processor    TEXT NOT NULL CHECK (processor IN ('stripe', 'paypal', 'square')),
    amount       NUMERIC(10,2) NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'failed')),
    processor_id TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bookings (
    id          SERIAL PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    type        TEXT,
    datetime    TIMESTAMPTZ,
    notes       TEXT,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bookings_user_id ON bookings(user_id);

CREATE TABLE IF NOT EXISTS expenses (
    id          SERIAL PRIMARY KEY,
    company     TEXT NOT NULL CHECK (company IN ('dod', 'rosesli')),
    category    TEXT,
    amount      NUMERIC(10,2) NOT NULL,
    description TEXT,
    date        DATE NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_expenses_company ON expenses(company);
