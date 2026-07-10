-- 014: Profile restructure — split name and address fields (rosesli).
-- Idempotent: safe to re-run.
--
-- full_name stays authoritative for existing consumers (emails, job snapshots,
-- offers, autocomplete) and is recomposed "First M. Last" on every profile
-- save. Lists display "Last, First" from the split columns when present.
-- The legacy free-text address column is kept; once any split address field
-- is saved the split fields win in the UI.
--
-- Backfill is rosesli-only so dod list displays are untouched: first token →
-- first_name, last token → last_name, a single-letter middle token (with or
-- without a trailing period) → middle_initial.

ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS first_name     TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS last_name      TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS middle_initial TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS address_street TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS address_city   TEXT;
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS address_state  TEXT;

UPDATE portal_users AS pu SET
  first_name = s.parts[1],
  last_name  = CASE WHEN array_length(s.parts, 1) >= 2
                    THEN s.parts[array_length(s.parts, 1)] END,
  middle_initial = CASE WHEN array_length(s.parts, 1) >= 3
                         AND length(rtrim(s.parts[2], '.')) = 1
                    THEN upper(rtrim(s.parts[2], '.')) END
FROM (
  SELECT id AS pid, regexp_split_to_array(trim(full_name), '\s+') AS parts
  FROM portal_users
  WHERE full_name IS NOT NULL AND trim(full_name) <> ''
) AS s
WHERE pu.id = s.pid
  AND pu.company = 'rosesli'
  AND pu.first_name IS NULL;
