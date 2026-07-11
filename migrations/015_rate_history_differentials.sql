-- 015: Effective-dated rate history + DB-backed differentials (rosesli).
-- Idempotent: safe to re-run.
--
-- Rate changes apply by SERVICE DATE: rate_for(user, date) resolves the latest
-- rate_history row with effective_date <= date, falling back to
-- portal_users.interpreter_rate (which stays synced to the currently-effective
-- rate so autocomplete APIs keep working). Backfill = one row per rated user
-- at 1970-01-01 so every existing job/invoice resolves to today's rate.

CREATE TABLE IF NOT EXISTS rate_history (
    id             SERIAL PRIMARY KEY,
    company        TEXT NOT NULL,
    user_id        INTEGER NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    rate           NUMERIC(10,2) NOT NULL,
    effective_date DATE NOT NULL,
    created_by     INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, effective_date)
);
CREATE INDEX IF NOT EXISTS idx_rate_history_user ON rate_history(user_id, effective_date DESC);

INSERT INTO rate_history (company, user_id, rate, effective_date)
SELECT company, id, interpreter_rate, DATE '1970-01-01'
FROM portal_users
WHERE interpreter_rate IS NOT NULL
ON CONFLICT (user_id, effective_date) DO NOTHING;

-- Differentials move from the hardcoded DIFFERENTIALS dict to the DB so Amanda
-- can self-serve pricing (Phase 8 adds the settings page). sort_order preserves
-- the dict order — the invoice-form auto-select maps day/evening/overnight to
-- fixed positions 0–5. specialty_* rows are $0 placeholders, excluded from the
-- invoice dropdowns until Phase 8 prices them.

CREATE TABLE IF NOT EXISTS differentials (
    id             SERIAL PRIMARY KEY,
    company        TEXT NOT NULL,
    code           TEXT NOT NULL,
    label          TEXT NOT NULL,
    amount         NUMERIC(10,2) NOT NULL DEFAULT 0,
    effective_date DATE NOT NULL DEFAULT '1970-01-01',
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (company, code, effective_date)
);

INSERT INTO differentials (company, code, label, amount, effective_date, sort_order) VALUES
  ('rosesli', 'day',                  'Daytime / Weekdays 7a–5p',      0, '1970-01-01', 10),
  ('rosesli', 'weekend_day',          'Weekend Day (Sat/Sun) 7a–5p',   5, '1970-01-01', 20),
  ('rosesli', 'weekday_evening',      'Weekday Evening 5p–10p',        5, '1970-01-01', 30),
  ('rosesli', 'weekend_evening',      'Weekend Evening 5p–10p',       10, '1970-01-01', 40),
  ('rosesli', 'overnight',            'Overnight 10p–7a',             10, '1970-01-01', 50),
  ('rosesli', 'weekend_overnight',    'Weekend Overnight 10p–7a',     15, '1970-01-01', 60),
  ('rosesli', 'conference',           'Conference',                   15, '1970-01-01', 70),
  ('rosesli', 'holiday',              'Holiday',                      12, '1970-01-01', 80),
  ('rosesli', 'lmr',                  'LMR (<24 hr notice)',          10, '1970-01-01', 90),
  ('rosesli', 'specialty_k12',        'K-12',                          0, '1970-01-01', 110),
  ('rosesli', 'specialty_higher_ed',  'Higher Ed',                     0, '1970-01-01', 120),
  ('rosesli', 'specialty_conference', 'Conference (Specialty)',        0, '1970-01-01', 130),
  ('rosesli', 'specialty_performance','Performance',                   0, '1970-01-01', 140),
  ('rosesli', 'specialty_deafblind',  'Deafblind',                     0, '1970-01-01', 150),
  ('rosesli', 'specialty_government', 'Government',                    0, '1970-01-01', 160)
ON CONFLICT (company, code, effective_date) DO NOTHING;
