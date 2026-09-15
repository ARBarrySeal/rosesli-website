# Phase 1 — Interpreter Invoice Lifecycle + Expenses — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an interpreter create an individual, job-linked invoice with auto-computed differential lines (split proportionally across time bands), attach informational expense entries, edit it before submission, and bulk-submit multiple invoices at once from a split (draft / submitted) list — implementing items 1.1–1.8 of `docs/portal-feature-batch-2026-07-22.md`.

**Architecture:** Extend the existing `invoices` table and the existing `/portal/admin/invoices/create` + `/portal/invoices` + `/portal/invoices/<id>` routes/templates (already used by both admins and interpreters — see `portal_admin.py:103` and `portal_pages.py:889`) rather than building a parallel system. Add a `job_id` link so an invoice can be tied to one real assignment; add a pure function that splits a job's hours across the six time-band differentials (day/evening/overnight × weekday/weekend) and makes the server (not the client JS) authoritative for the computed lines and total whenever a job is selected. The pre-existing freeform (no-job) manual entry path is untouched for backward compatibility.

**Tech Stack:** Flask (Blueprints in `portal_*.py`), psycopg2 raw SQL (`portal_db.py`), Jinja2 templates, vanilla JS (no frontend framework), pytest + `pytest-flask` (`tests/test_phase*.py` convention), Postgres 16 (local dev via Docker container `postgres_botdb`, db `botdb`, user `botuser`).

## Global Constraints

- Money math is server-authoritative: whenever `job_id` is present, ignore client-submitted duration/rate/lines and recompute everything from the job row + `portal_rates.rate_for()` + the new time-band splitter. Client JS may only *preview*.
- 2-hour minimum billing (existing rule, currently JS-only in `templates/portal_admin_invoice_create.html:193`: `(hrs > 0 && hrs < 2) ? 2 : hrs`) must be preserved for the job-linked auto path: if the job's actual duration is under 2 hours, scale the split proportionally so total billed hours = 2.
- Expenses are informational only (type + free-text note, no dollar amount) per the resolved decision on 2026-07-23 — they never affect `invoices.amount`.
- A job can be linked to at most one invoice (partial unique index on `job_id`), mirroring the existing `uq_invoice_jobs_job` pattern in `migrations/012_invoice_jobs_unique_job.sql`.
- Once `invoices.submitted = TRUE`, the invoice is locked: no edit route may modify it (existing `submit_invoice` in `portal_pages.py:1101` already sets this flag; this plan does not change that route's core behavior beyond adding a notification email).
- Follow existing code conventions exactly: dict rows from `portal_db.query_one/query_all`, `portal_db.transaction()` for multi-statement writes, `audit_log(...)` from `portal_audit.py` on every mutating action, inline `style=` attributes matching the existing templates (no new CSS framework), `nonce="{{ csp_nonce() }}"` on every `<script>` tag.
- Test DB: local Postgres container `postgres_botdb`, role `botuser` / password `password`, database `botdb` (already provisioned with all 18 existing migrations for this session — run new migrations the same way: `docker exec -i postgres_botdb psql -U botuser -d botdb -v ON_ERROR_STOP=1 < migrations/019_....sql`). Run tests with:
  `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/ -q`
- Do not touch `test_portal_security.py::test_admin_login_without_mfa_redirects_to_enroll`, `test_admin_with_enroll_cookie_blocked_from_admin_routes`, or `test_rate_history.py::test_client_rate_change_recalcs_future_unpaid_only` — these 3 failures are pre-existing (unrelated MFA config + a date-relative test broken by real time passing 2026-07-10), not caused by or in scope for this work.
- Never delete a row from `docs/portal-feature-batch-2026-07-22.md`; only flip status glyphs (⬜ → 🟡 → ✅) and append to the Progress log.

---

### Task 1: Migration — `job_id` link + `expenses` column on `invoices`

**Files:**
- Create: `migrations/019_invoice_job_link.sql`
- Test: `tests/test_phase9_invoice_lifecycle.py` (new file, created in this task, extended by later tasks)

**Interfaces:**
- Produces: `invoices.job_id` (INT, nullable, FK → `jobs(id)` ON DELETE SET NULL), `invoices.expenses` (TEXT, nullable, JSON-encoded list of `{"type": str, "note": str}`), unique partial index `uq_invoices_job_id`.

- [ ] **Step 1: Write the migration**

```sql
-- 019: Phase 1 — link an invoice to the specific job it bills, and add an
-- informational Expenses section. job_id is ON DELETE SET NULL so deleting an
-- assignment never drops a historical invoice. The partial unique index
-- mirrors migrations/012_invoice_jobs_unique_job.sql: a job can be billed by
-- at most one individual invoice, so re-creating an invoice for the same job
-- (double-click, two tabs) is rejected at the DB level.
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS expenses TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_job_id
    ON invoices(job_id) WHERE job_id IS NOT NULL;
```

- [ ] **Step 2: Apply it to the local dev DB**

Run: `docker exec -i postgres_botdb psql -U botuser -d botdb -v ON_ERROR_STOP=1 < migrations/019_invoice_job_link.sql`
Expected: `ALTER TABLE` ×2, `CREATE INDEX`

- [ ] **Step 3: Write the failing test file with a schema smoke test**

```python
"""Phase 1 (2026-07-22 batch) — individual job-linked invoices, auto-split
differentials, expenses, edit/lock, list split, bulk submit.
"""
import json
import secrets
from datetime import date, time

import pytest

import portal_db
from portal_auth import hash_password

COMPANY = "rosesli"
PW = "PytestPhase9_12345!"

ADMIN_EMAIL = "pytest-p9-admin@example.test"
INTERP_EMAIL = "pytest-p9-interp@example.test"
INTERP2_EMAIL = "pytest-p9-interp2@example.test"
EMAILS = [ADMIN_EMAIL, INTERP_EMAIL, INTERP2_EMAIL]


def _cleanup():
    uids = [r["id"] for r in portal_db.query_all(
        "SELECT id FROM portal_users WHERE email = ANY(%s) AND company = %s",
        (EMAILS, COMPANY))]
    if uids:
        portal_db.execute("DELETE FROM invoices WHERE user_id = ANY(%s)", (uids,))
        portal_db.execute("DELETE FROM jobs WHERE company = %s AND "
                           "(interpreter_1_id = ANY(%s) OR client_id = ANY(%s))",
                           (COMPANY, uids, uids))
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY))
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))


def _mk_user(email, role, **cols):
    base = "INSERT INTO portal_users (email, password_hash, full_name, role, company, active"
    vals = [email, hash_password(PW), cols.pop("full_name", f"Pytest {role.title()}"),
            role, COMPANY, True]
    extra = ""
    for k, v in cols.items():
        extra += f", {k}"
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    row = portal_db.execute(
        f"{base}{extra}) VALUES ({placeholders}) RETURNING id", tuple(vals))
    return row["id"]


def _mk_job(interpreter_id, event_date, start_time, end_time, status="confirmed"):
    row = portal_db.execute(
        "INSERT INTO jobs (company, event_date, start_time, end_time, duration, "
        "  status, interpreter_1_id, job_number, setting) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (COMPANY, event_date, start_time, end_time,
         _hours_between(start_time, end_time), status, interpreter_id,
         f"J-{secrets.token_hex(3)}", "Medical"),
    )
    return row["id"]


def _hours_between(start_time, end_time):
    s = start_time.hour * 60 + start_time.minute
    e = end_time.hour * 60 + end_time.minute
    if e <= s:
        e += 24 * 60
    return round((e - s) / 60.0, 2)


@pytest.fixture
def world():
    _cleanup()
    ids = {
        "admin": _mk_user(ADMIN_EMAIL, "admin"),
        "interp": _mk_user(INTERP_EMAIL, "employee", interpreter_rate=50),
        "interp2": _mk_user(INTERP2_EMAIL, "employee", interpreter_rate=50),
    }
    yield ids
    _cleanup()


def _login(client, email):
    with client.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        csrf = sess["csrf_token"]
    r = client.post("/login", data={"email": email, "password": PW, "csrf_token": csrf})
    assert r.status_code == 200, r.data
    return client


def _client(app, email):
    return _login(app.test_client(), email)


def _csrf(client):
    with client.session_transaction() as sess:
        return sess["csrf_token"]


# ── 1. Schema ──────────────────────────────────────────────────────────────

def test_invoices_table_has_job_id_and_expenses_columns():
    cols = {r["column_name"] for r in portal_db.query_all(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'invoices'")}
    assert "job_id" in cols
    assert "expenses" in cols


def test_job_id_unique_index_rejects_second_invoice_for_same_job(world):
    jid = _mk_job(world["interp"], date(2026, 8, 3), time(9, 0), time(11, 0))
    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, job_id) VALUES (%s, 10, 'unpaid', %s)",
        (world["interp"], jid))
    with pytest.raises(Exception):
        portal_db.execute(
            "INSERT INTO invoices (user_id, amount, status, job_id) VALUES (%s, 10, 'unpaid', %s)",
            (world["interp"], jid))
```

- [ ] **Step 4: Run it to verify it fails only on missing fixtures, not the schema assertions**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -q`
Expected: both tests in this task PASS (the migration already applied in Step 2). If `test_job_id_unique_index_rejects_second_invoice_for_same_job` fails to raise, the index did not apply — re-check Step 2.

- [ ] **Step 5: Commit**

```bash
git add migrations/019_invoice_job_link.sql tests/test_phase9_invoice_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(portal): add job_id link + expenses column to invoices (Phase 1.1/1.5 schema)

One invoice per job (partial unique index, mirrors invoice_jobs). expenses
is informational-only text (JSON list of {type, note}), never priced.
EOF
)"
```

---

### Task 2: `portal_rates.compute_time_band_hours` — pure differential splitter

**Files:**
- Modify: `portal_rates.py` (append function; imports already has `from datetime import date`, add `datetime, timedelta`)
- Test: `tests/test_phase9_invoice_lifecycle.py` (append)

**Interfaces:**
- Consumes: nothing external — pure function.
- Produces: `compute_time_band_hours(event_date: date, start_time: time, end_time: time) -> dict[str, float]`, keyed by one of `{"day", "weekend_day", "weekday_evening", "weekend_evening", "overnight", "weekend_overnight"}`, values are hours (float, rounded to 2 dp), summing to the shift's total billable hours (with the 2-hour minimum applied to the total before splitting). Used by Task 4.

- [ ] **Step 1: Write the failing tests**

```python
# ── 2. Time-band splitter ─────────────────────────────────────────────────

from portal_rates import compute_time_band_hours  # add to top imports


def test_split_pure_daytime_weekday():
    # Wed 2026-08-05, 9am-1pm: entirely inside 7a-5p on a weekday
    bands = compute_time_band_hours(date(2026, 8, 5), time(9, 0), time(13, 0))
    assert bands == {"day": 4.0}


def test_split_crosses_day_into_evening():
    # Wed 2026-08-05, 4pm-8pm: 1hr day (4-5p) + 3hr evening (5-8p)
    bands = compute_time_band_hours(date(2026, 8, 5), time(16, 0), time(20, 0))
    assert bands == {"day": 1.0, "weekday_evening": 3.0}


def test_split_overnight_crossing_midnight():
    # Fri 2026-08-07 11pm -> Sat 2026-08-08 2am: both portions are "overnight"
    # band by time-of-day, but the post-midnight portion is a WEEKEND day, so
    # it becomes weekend_overnight while the pre-midnight portion (still
    # Friday, a weekday) stays overnight.
    bands = compute_time_band_hours(date(2026, 8, 7), time(23, 0), time(2, 0))
    assert bands == {"overnight": 1.0, "weekend_overnight": 2.0}


def test_split_weekend_daytime():
    # Sat 2026-08-08, 10am-2pm
    bands = compute_time_band_hours(date(2026, 8, 8), time(10, 0), time(14, 0))
    assert bands == {"weekend_day": 4.0}


def test_split_applies_two_hour_minimum_proportionally():
    # Wed 2026-08-05, 9am-9:30am = 0.5h actual, entirely daytime.
    # Under the 2-hour minimum, billed hours scale to 2.0, still all "day"
    # since the shift never leaves that band.
    bands = compute_time_band_hours(date(2026, 8, 5), time(9, 0), time(9, 30))
    assert bands == {"day": 2.0}


def test_split_two_hour_minimum_scales_multi_band_proportionally():
    # Wed 2026-08-05, 4:45pm-5:15pm = 0.5h actual: 0.25h day + 0.25h evening.
    # Scaled to a 2h minimum (4x), each band scales to 1.0h.
    bands = compute_time_band_hours(date(2026, 8, 5), time(16, 45), time(17, 15))
    assert bands == {"day": 1.0, "weekday_evening": 1.0}
```

- [ ] **Step 2: Run to verify failure**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -k split -q`
Expected: FAIL with `ImportError: cannot import name 'compute_time_band_hours'`

- [ ] **Step 3: Implement it**

Append to `portal_rates.py` (add `from datetime import date, time` stays; no new imports needed beyond what's there):

```python
# ── Time-band differential splitting (Phase 1, 2026-07-22 batch) ───────────
# The six time-band differential codes from migrations/015: each is
# (weekday|weekend) x (day 7a-5p | evening 5p-10p | overnight 10p-7a).
# Non-time-derivable differentials (holiday, lmr, conference, specialty_*)
# are NOT computed here — they stay manual "+ Add Line" additions on the
# invoice form, same as today.
_MIN_BILLABLE_HOURS = 2.0
_BAND_BOUNDARIES_MIN = (0, 7 * 60, 17 * 60, 22 * 60, 24 * 60)  # midnight,7a,5p,10p,midnight


def _band_for_minute_of_day(minute_of_day, is_weekend):
    # minute_of_day in [0, 1440); overnight wraps both [0,7a) and [10p,24:00)
    if minute_of_day < 7 * 60 or minute_of_day >= 22 * 60:
        band = "overnight"
    elif minute_of_day < 17 * 60:
        band = "day"
    else:
        band = "weekday_evening" if not is_weekend else "weekend_evening"
        return band
    return ("weekend_" + band) if is_weekend and band != "overnight" else \
           ("weekend_overnight" if is_weekend else band)


def compute_time_band_hours(event_date, start_time, end_time):
    """Split a shift into hours per time-band differential code, splitting
    proportionally at every 7a/5p/10p/midnight boundary it crosses. Midnight
    crossings can also flip weekday->weekend (or vice versa), which changes
    the code for the portion after midnight. If the shift's actual duration
    is under the 2-hour minimum, every band's hours are scaled up so the
    total equals exactly 2.0 (proportional to the actual split), matching
    the existing 2-hour-minimum billing rule."""
    start_min = start_time.hour * 60 + start_time.minute
    end_min = end_time.hour * 60 + end_time.minute
    if end_min <= start_min:
        end_min += 24 * 60  # crosses midnight

    # Boundaries: every 7a/5p/10p/midnight instant between start and end.
    boundaries = {start_min, end_min}
    day_offset = 0
    while day_offset * 1440 < end_min:
        for b in _BAND_BOUNDARIES_MIN:
            point = day_offset * 1440 + b
            if start_min < point < end_min:
                boundaries.add(point)
        day_offset += 1
    points = sorted(boundaries)

    totals = {}
    for i in range(len(points) - 1):
        seg_start, seg_end = points[i], points[i + 1]
        mid = (seg_start + seg_end) / 2.0
        day_num = int(mid // 1440)  # 0 = event_date, 1 = event_date + 1
        minute_of_day = mid % 1440
        seg_date = event_date + timedelta(days=day_num)
        is_weekend = seg_date.weekday() >= 5  # Sat=5, Sun=6
        code = _band_for_minute_of_day(minute_of_day, is_weekend)
        hours = (seg_end - seg_start) / 60.0
        totals[code] = totals.get(code, 0.0) + hours

    actual_total = sum(totals.values())
    if 0 < actual_total < _MIN_BILLABLE_HOURS:
        scale = _MIN_BILLABLE_HOURS / actual_total
        totals = {k: v * scale for k, v in totals.items()}

    return {k: round(v, 2) for k, v in totals.items()}
```

Add `from datetime import date, time, timedelta` at the top of `portal_rates.py` (currently only `from datetime import date`).

- [ ] **Step 4: Run tests, verify pass**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -k split -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add portal_rates.py tests/test_phase9_invoice_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(portal): auto-split job hours across time-band differentials

Pure function computing day/evening/overnight x weekday/weekend hours for
a shift, splitting proportionally at each band boundary crossed and at
midnight (which can also flip weekday/weekend). Preserves the existing
2-hour minimum billing rule, scaled proportionally across bands.
EOF
)"
```

---

### Task 3: Shared "billable jobs" query with job-linked-invoice exclusion

**Files:**
- Modify: `portal_interpreter_invoices.py:48-73` (the existing `billable_jobs_for_interpreter` function)
- Test: `tests/test_phase9_invoice_lifecycle.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `billable_jobs_for_interpreter(company, uid)` — same signature and return shape as today (list of dicts with `id, job_number, event_date, start_time, end_time, setting, duration, event_address, status, rate, line_amount`), but now ALSO excludes jobs already linked via `invoices.job_id` (Task 1's new column), not just jobs in `invoice_jobs`. Task 4 reuses this for the job-select dropdown.

- [ ] **Step 1: Write the failing test**

```python
# ── 3. Billable jobs excludes already-invoiced-by-job_id assignments ───────

def test_billable_jobs_excludes_job_linked_invoice(world):
    from portal_interpreter_invoices import billable_jobs_for_interpreter
    jid = _mk_job(world["interp"], date(2026, 8, 10), time(9, 0), time(11, 0))
    before = billable_jobs_for_interpreter(COMPANY, world["interp"])
    assert any(j["id"] == jid for j in before)

    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, job_id) VALUES (%s, 100, 'unpaid', %s)",
        (world["interp"], jid))

    after = billable_jobs_for_interpreter(COMPANY, world["interp"])
    assert not any(j["id"] == jid for j in after)
```

- [ ] **Step 2: Run to verify it fails**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -k job_linked_invoice -q`
Expected: FAIL — `jid` still present in `after` (the query doesn't know about `invoices.job_id` yet)

- [ ] **Step 3: Modify the query**

In `portal_interpreter_invoices.py`, change the `WHERE` clause inside `billable_jobs_for_interpreter` (currently at line ~65-66):

```python
        "  AND id NOT IN (SELECT job_id FROM invoice_jobs WHERE job_id IS NOT NULL) "
        "ORDER BY event_date NULLS LAST, start_time",
```

to:

```python
        "  AND id NOT IN (SELECT job_id FROM invoice_jobs WHERE job_id IS NOT NULL) "
        "  AND id NOT IN (SELECT job_id FROM invoices WHERE job_id IS NOT NULL) "
        "ORDER BY event_date NULLS LAST, start_time",
```

- [ ] **Step 4: Run tests, verify pass**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -q`
Expected: all tests so far pass (this task's + Tasks 1-2's)

- [ ] **Step 5: Commit**

```bash
git add portal_interpreter_invoices.py tests/test_phase9_invoice_lifecycle.py
git commit -m "fix(portal): exclude job_id-linked invoices from billable-jobs list"
```

---

### Task 4: Job-linked auto-invoice creation (server-authoritative)

**Files:**
- Modify: `portal_admin.py:103-201` (`create_invoice` route)
- Modify: `templates/portal_admin_invoice_create.html` (add job-select dropdown + read-only computed display; keep the freeform path untouched when no job is selected)
- Test: `tests/test_phase9_invoice_lifecycle.py` (append)

**Interfaces:**
- Consumes: `portal_rates.compute_time_band_hours` (Task 2), `portal_rates.rate_for` (existing), `portal_rates.differentials_for` (existing), `portal_interpreter_invoices.billable_jobs_for_interpreter` (Task 3).
- Produces: POST `/portal/admin/invoices/create` accepts an optional `job_id` field. When present and valid, the route ignores `date_of_service`/`start_time`/`end_time`/`base_rate`/`differential`/`duration_hours`/`amount`/extra-line fields from the form entirely and computes them server-side. Primary band (base_rate/differential/rate_applied/duration_hours) = the band the shift **starts** in; any additional bands go into `interpreter_rates` extra lines, same JSON shape as `parse_extra_lines` already produces (`{"differential": <numeric addon>, "duration": <hours>, "amount": <dollars>}`, tagged with `"code"` via a new optional key so later tooling can identify auto lines — reuse the existing key name pattern, add `"auto": true`). `amount` = sum of all band amounts. GET renders the job dropdown (employee: `billable_jobs_for_interpreter(company, uid)`; admin: same, scoped to the selected interpreter via a small AJAX-free re-render, matching existing admin/interpreter branching already in the route).

- [ ] **Step 1: Write the failing test**

```python
# ── 4. Job-linked auto-invoice ──────────────────────────────────────────────

def test_create_invoice_from_job_auto_computes_lines(app, world):
    c = _client(app, INTERP_EMAIL)
    jid = _mk_job(world["interp"], date(2026, 8, 5), time(16, 0), time(20, 0))  # 1h day + 3h evening
    r = c.post("/portal/admin/invoices/create", data={
        "csrf_token": _csrf(c),
        "job_id": str(jid),
        # These should be IGNORED server-side because job_id is present:
        "amount": "1", "base_rate": "1", "duration_hours": "1",
    }, follow_redirects=False)
    assert r.status_code == 302, r.data

    inv = portal_db.query_one(
        "SELECT * FROM invoices WHERE job_id = %s", (jid,))
    assert inv is not None
    assert float(inv["base_rate"]) == 50.0          # interp's rate, not the posted "1"
    assert float(inv["duration_hours"]) == 1.0       # primary band = day (shift starts there)
    lines = json.loads(inv["interpreter_rates"])
    assert len(lines) == 1
    assert lines[0]["duration"] == 3.0               # evening band as an extra line
    # total = 1h*$50 (day) + 3h*($50+$5 evening) = $50 + $165 = $215
    assert float(inv["amount"]) == 215.0


def test_create_invoice_from_job_rejects_someone_elses_job(app, world):
    c = _client(app, INTERP2_EMAIL)
    jid = _mk_job(world["interp"], date(2026, 8, 5), time(9, 0), time(11, 0))
    r = c.post("/portal/admin/invoices/create", data={
        "csrf_token": _csrf(c), "job_id": str(jid),
    }, follow_redirects=False)
    assert portal_db.query_one("SELECT id FROM invoices WHERE job_id = %s", (jid,)) is None
    assert r.status_code == 200  # re-renders with an error, no invoice created


def test_create_invoice_from_job_rejects_already_invoiced_job(app, world):
    c = _client(app, INTERP_EMAIL)
    jid = _mk_job(world["interp"], date(2026, 8, 5), time(9, 0), time(11, 0))
    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, job_id) VALUES (%s, 100, 'unpaid', %s)",
        (world["interp"], jid))
    r = c.post("/portal/admin/invoices/create", data={
        "csrf_token": _csrf(c), "job_id": str(jid),
    }, follow_redirects=False)
    n = portal_db.query_one("SELECT COUNT(*) AS n FROM invoices WHERE job_id = %s", (jid,))["n"]
    assert n == 1  # still just the one pre-existing invoice
```

- [ ] **Step 2: Run to verify failure**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -k create_invoice_from_job -q`
Expected: FAIL — `job_id` column not consulted by the route yet, `inv` is None

- [ ] **Step 3: Implement the route change**

In `portal_admin.py`, inside `create_invoice` (POST branch, after the existing form-field parsing block that currently starts at `amount_raw = ...`), insert job-linked handling. Replace the block from `amount_raw = (request.form.get("amount") or "").strip()` (line ~125) through the `interpreter_rates = _json.dumps(extra_lines) if extra_lines else None` line (line ~156) with:

```python
    import json as _json
    from portal_interpreter_invoices import billable_jobs_for_interpreter

    job_id_raw = (request.form.get("job_id") or "").strip()
    job_id = int(job_id_raw) if job_id_raw.isdigit() else None

    if job_id:
        # Server-authoritative: recompute everything from the job row, never
        # trust client-submitted amount/rate/lines for a job-linked invoice.
        if is_admin:
            uid_raw = (request.form.get("user_id") or "").strip()
            target = portal_db.query_one(
                "SELECT id FROM portal_users WHERE id = %s AND company = %s "
                "AND role = 'employee'",
                (uid_raw, company)) if uid_raw.isdigit() else None
            if not target:
                return render_template("portal_admin_invoice_create.html", interpreters=interpreters,
                                       diff_options_json=diffs_json,
                                       error="Invalid interpreter.")
            recipient_id = target["id"]
        else:
            recipient_id = int(g.user["sub"])
        billable = {j["id"]: j for j in billable_jobs_for_interpreter(company, recipient_id)}
        job = billable.get(job_id)
        if not job or not job.get("event_date") or not job.get("start_time") \
                or not job.get("end_time"):
            return render_template("portal_admin_invoice_create.html", interpreters=interpreters,
                                   diff_options_json=diffs_json,
                                   error="That assignment isn't available to invoice.")
        rate = portal_rates.rate_for(recipient_id, job["event_date"])
        bands = portal_rates.compute_time_band_hours(
            job["event_date"], job["start_time"], job["end_time"])
        diff_amounts = {r["code"]: float(r["amount"])
                        for r in portal_rates.differentials_for(company, job["event_date"])}
        # Primary band = whichever band the shift starts in. compute_time_band_hours
        # builds `bands` by walking the shift chronologically and inserting each
        # code the first time it's encountered, so dict insertion order (stable
        # since Python 3.7) already gives us "first band touched" for free —
        # no separate start-time matching needed (and a substring-based matcher
        # would be wrong anyway: "day" is a substring of both "weekday_evening"
        # and "weekend_day", so it can't distinguish bands reliably).
        primary_code = next(iter(bands))
        base_rate = rate or 0.0
        diff_val = diff_amounts.get(primary_code, 0.0)
        duration_hours = bands.pop(primary_code)
        rate_applied = base_rate + diff_val
        amount = round(duration_hours * rate_applied, 2)
        extra_lines = []
        for code, hrs in bands.items():
            addon = diff_amounts.get(code, 0.0)
            line_amt = round(hrs * (base_rate + addon), 2)
            extra_lines.append({"differential": addon, "duration": hrs,
                                "amount": line_amt, "code": code, "auto": True})
            amount += line_amt
        interpreter_rates = _json.dumps(extra_lines) if extra_lines else None
        date_of_service = job["event_date"]
        start_time = job["start_time"].strftime("%H:%M")
        end_time = job["end_time"].strftime("%H:%M")
        description = f"Assignment {job.get('job_number') or ('#' + str(job_id))}"
        due_date = None
        notes = None
        diff_raw = str(diff_val)
    else:
        amount_raw      = (request.form.get("amount") or "").strip()
        description     = (request.form.get("description") or "").strip()
        due_date        = request.form.get("due_date") or None
        notes           = (request.form.get("notes") or "").strip()
        date_of_service = request.form.get("date_of_service") or None
        start_time      = (request.form.get("start_time") or "").strip() or None
        end_time        = (request.form.get("end_time") or "").strip() or None
        base_rate_raw   = (request.form.get("base_rate") or "").strip()
        diff_raw        = (request.form.get("differential") or "0").strip()
        dur_raw         = (request.form.get("duration_hours") or "").strip()
        try:
            base_rate = float(base_rate_raw) if base_rate_raw else None
        except ValueError:
            base_rate = None
        try:
            diff_val = float(diff_raw) if diff_raw else 0.0
        except ValueError:
            diff_val = 0.0
        try:
            duration_hours = float(dur_raw) if dur_raw else None
        except ValueError:
            duration_hours = None
        rate_applied = (base_rate or 0) + diff_val if base_rate is not None else None

        from portal_client_invoices import parse_extra_lines
        extra_lines = parse_extra_lines(
            request.form,
            ("extra_differential_", "extra_duration_", "extra_amount_",
             "extra_date_", "extra_code_"),
            company, main_date=date_of_service)
        interpreter_rates = _json.dumps(extra_lines) if extra_lines else None
```

Add near the top of `portal_admin.py` (module level, alongside other imports): `import portal_rates`.

Further down, the *existing* `is_admin` / `user_id` validation block (original lines ~158-171: `if is_admin: user_id = request.form.get("user_id") ...`) is now redundant with the validation just added inside the `if job_id:` branch above — guard the existing block so it only runs for the freeform path, since the job_id path already resolved and validated `recipient_id`:

```python
    if not job_id:
        if is_admin:
            user_id = request.form.get("user_id") or ""
            if not user_id:
                return render_template("portal_admin_invoice_create.html", interpreters=interpreters,
                                       diff_options_json=diffs_json,
                                       error="Please select an interpreter.")
            target = portal_db.query_one(
                "SELECT id FROM portal_users WHERE id = %s AND company = %s AND role = 'employee'",
                (int(user_id), company),
            )
            if not target:
                return render_template("portal_admin_invoice_create.html", interpreters=interpreters,
                                       diff_options_json=diffs_json,
                                       error="Invalid interpreter.")
            recipient_id = int(user_id)
        else:
            recipient_id = int(g.user["sub"])
```

The final `amount` validation/INSERT block stays as-is EXCEPT: when `job_id` is set, skip the `amount_raw` float-parsing/validation entirely (amount is already a computed float) — wrap the existing block:

```python
    if job_id:
        pass  # amount already computed above
    else:
        try:
            amount = float(amount_raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            return render_template("portal_admin_invoice_create.html", interpreters=interpreters,
                                   diff_options_json=diffs_json,
                                   error="Amount must be a positive number.")

    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, description, due_date, interpreter_rates, notes, "
        "  date_of_service, service_start_time, service_end_time, duration_hours, "
        "  base_rate, differential, rate_applied, job_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (recipient_id, amount, description or None, due_date or None,
         interpreter_rates, notes or None,
         date_of_service or None, start_time, end_time, duration_hours,
         base_rate, diff_raw or None, rate_applied, job_id),
    )
```

Note the `recipient_id`/`is_admin`/`user_id` validation block that already exists between these two pieces (lines ~158-174 in the original) is unchanged — it still runs for both paths, just make sure `recipient_id` computed in the `job_id` branch above isn't clobbered (the existing admin-validation block re-derives `recipient_id` for the non-`job_id` path only — guard it with `if not job_id:` around the existing `if is_admin: user_id = ...` block, since the job_id branch already validated and set `recipient_id`).

- [ ] **Step 4: Update the GET branch to pass jobs for the dropdown**

Just above `if request.method == "GET":` in the same function, add:

```python
    my_jobs = billable_jobs_for_interpreter(company, int(g.user["sub"])) if not is_admin else []
```//: only computed for GET is fine since POST doesn't need it on success (redirect) and re-renders on error already have `interpreters`/`diffs_json` — add `jobs=my_jobs` to every `render_template("portal_admin_invoice_create.html", ...)` call in this function (there are 5: the GET, the two admin-validation-error ones, the amount-validation-error one, and the final success one).

- [ ] **Step 5: Add the job dropdown to the template**

In `templates/portal_admin_invoice_create.html`, insert immediately after the "Billing As" / interpreter-select block (after line 41, before the "Date of Service" grid at line 43), only for the employee (non-admin) role — matches the resolved decision that this flow lives on the interpreter's own page:

```html
  {% if g.user.role != 'admin' and jobs %}
  <label style="display:flex;flex-direction:column;gap:.35rem;font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);">
    Assignment <span style="text-transform:none;font-size:.65rem;">(auto-fills date, time, rate &amp; differentials)</span>
    <select name="job_id" id="job-select"
            style="background:var(--bg);border:1px solid var(--border);color:var(--text);
                   font-family:var(--font);padding:.5rem .75rem;border-radius:var(--radius);font-size:13px;">
      <option value="">— manual entry (no assignment) —</option>
      {% for j in jobs %}
      <option value="{{ j.id }}">
        {{ j.event_date.strftime('%b %d, %Y') if j.event_date else '—' }} — {{ j.setting or j.job_number or 'Assignment' }}
      </option>
      {% endfor %}
    </select>
  </label>
  {% endif %}
```

Then, in the `<script>` block, disable the manual fields when a job is selected (so users aren't misled into thinking they can edit auto-computed values) by adding near the top of the IIFE:

```javascript
  var jobSelect = document.getElementById('job-select');
  function syncJobMode() {
    var picked = jobSelect && jobSelect.value;
    [baseRate, diffSel, durHrs, amountIn, startTime, endTime,
     document.getElementById('date-of-service')].forEach(function (el) {
      if (el) el.disabled = !!picked;
    });
    if (addLineBtn) addLineBtn.disabled = !!picked;
  }
  if (jobSelect) { jobSelect.addEventListener('change', syncJobMode); syncJobMode(); }
```

(Disabled fields don't POST — that's fine, since the server ignores them entirely when `job_id` is present.)

- [ ] **Step 6: Run tests, verify pass**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -q`
Expected: all tests pass

- [ ] **Step 7: Run the full suite to check for regressions**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/ -q`
Expected: same 3 pre-existing failures as the baseline (see Global Constraints), nothing new

- [ ] **Step 8: Commit**

```bash
git add portal_admin.py templates/portal_admin_invoice_create.html tests/test_phase9_invoice_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(portal): job-linked invoice creation with server-computed differentials

Selecting an assignment on the interpreter's Create Invoice form now
auto-fills and locks date/time/rate/differential lines/total, computed
server-side from the job + the Phase-1 time-band splitter. Freeform manual
entry (no assignment selected) is unchanged.
EOF
)"
```

---

### Task 5: Expenses section (create form, storage, detail display)

**Files:**
- Modify: `templates/portal_admin_invoice_create.html` (add Expenses UI)
- Modify: `portal_admin.py` (`create_invoice` — parse and store expenses)
- Modify: `templates/portal_invoice.html` (render expenses, read-only)
- Test: `tests/test_phase9_invoice_lifecycle.py` (append)

**Interfaces:**
- Produces: `invoices.expenses` populated from form fields `expense_type_<n>` / `expense_note_<n>` as a JSON list `[{"type": "Parking", "note": "..."}]`. Never affects `amount`.

- [ ] **Step 1: Write the failing test**

```python
# ── 5. Expenses (informational only, no dollar amount) ─────────────────────

def test_create_invoice_stores_expenses_without_affecting_amount(app, world):
    c = _client(app, INTERP_EMAIL)
    r = c.post("/portal/admin/invoices/create", data={
        "csrf_token": _csrf(c),
        "amount": "100", "base_rate": "50", "duration_hours": "2",
        "date_of_service": "2026-08-05",
        "expense_type_0": "Parking", "expense_note_0": "Downtown garage, $12",
        "expense_type_1": "Mileage", "expense_note_1": "22 miles round trip",
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    inv = portal_db.query_one(
        "SELECT * FROM invoices WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (world["interp"],))
    expenses = json.loads(inv["expenses"])
    assert expenses == [
        {"type": "Parking", "note": "Downtown garage, $12"},
        {"type": "Mileage", "note": "22 miles round trip"},
    ]
    assert float(inv["amount"]) == 100.0  # unaffected by expenses
```

- [ ] **Step 2: Run to verify failure**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -k stores_expenses -q`
Expected: FAIL — `inv["expenses"]` is `None`

- [ ] **Step 3: Parse expenses in the route**

In `portal_admin.py`, in `create_invoice`, right after the `job_id`/freeform branching block (before the final `if job_id: pass else: ... amount validation`), add:

```python
    expenses = []
    idx = 0
    while True:
        etype = (request.form.get(f"expense_type_{idx}") or "").strip()
        if not etype and f"expense_type_{idx}" not in request.form:
            break
        enote = (request.form.get(f"expense_note_{idx}") or "").strip()
        if etype:
            expenses.append({"type": etype, "note": enote})
        idx += 1
    expenses_json = _json.dumps(expenses) if expenses else None
```

Add `expenses` to the INSERT column list and `expenses_json` to the values tuple from Task 4's Step 3 final INSERT (append `, expenses` to the column list and `%s` to placeholders, `expenses_json` to the params tuple).

- [ ] **Step 4: Add the Expenses UI to the create template**

In `templates/portal_admin_invoice_create.html`, insert after the `#extra-lines` / `#add-line-btn` block (after line 125, before the "Job Notes" label at line 127):

```html
  {# Expenses — informational only (type + note), never priced #}
  <div id="expenses"></div>
  <button type="button" id="add-expense-btn"
          style="align-self:flex-start;background:none;border:1px dashed var(--border);color:var(--muted);
                 font-family:var(--font);padding:.4rem .85rem;border-radius:var(--radius);cursor:pointer;font-size:.78rem;">
    + Add Expense
  </button>
```

And in the `<script>` block, near the `add-line-btn` handler, add:

```javascript
  var expensesDiv = document.getElementById('expenses');
  var addExpenseBtn = document.getElementById('add-expense-btn');
  var EXPENSE_TYPES = ['Parking', 'Mileage', 'Travel Time', 'Other'];
  function reindexExpenses() {
    var rows = expensesDiv.querySelectorAll('.expense-row');
    for (var i = 0; i < rows.length; i++) {
      rows[i].querySelector('.expense-type').name = 'expense_type_' + i;
      rows[i].querySelector('.expense-note').name = 'expense_note_' + i;
    }
  }
  if (addExpenseBtn && expensesDiv) {
    addExpenseBtn.addEventListener('click', function () {
      var idx = expensesDiv.querySelectorAll('.expense-row').length;
      var row = document.createElement('div');
      row.className = 'expense-row';
      row.style.cssText = 'display:grid;grid-template-columns:1fr 2fr auto;gap:.5rem;align-items:end;margin-top:.5rem;';

      var typeSel = document.createElement('select');
      typeSel.className = 'expense-type'; typeSel.name = 'expense_type_' + idx;
      typeSel.style.cssText = 'background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--font);padding:.4rem .6rem;border-radius:var(--radius);font-size:13px;';
      EXPENSE_TYPES.forEach(function (t) {
        var opt = document.createElement('option'); opt.value = t; opt.textContent = t;
        typeSel.appendChild(opt);
      });

      var noteIn = document.createElement('input');
      noteIn.type = 'text'; noteIn.className = 'expense-note'; noteIn.name = 'expense_note_' + idx;
      noteIn.placeholder = 'Details…'; noteIn.maxLength = 300;
      noteIn.style.cssText = 'background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--font);padding:.4rem .6rem;border-radius:var(--radius);font-size:13px;';

      var rmBtn = document.createElement('button');
      rmBtn.type = 'button'; rmBtn.textContent = '✕';
      rmBtn.style.cssText = 'background:none;border:none;color:var(--muted);cursor:pointer;font-size:.9rem;padding:.4rem;';
      rmBtn.addEventListener('click', function () { row.remove(); reindexExpenses(); });

      row.appendChild(typeSel); row.appendChild(noteIn); row.appendChild(rmBtn);
      expensesDiv.appendChild(row);
    });
  }
```

- [ ] **Step 5: Render expenses on the detail page, and add the `from_json` filter**

In `templates/portal_invoice.html`, insert after the "Extra rate lines" block (after line 74, before "Bundled assignments" at line 76):

```html
  {# ── Expenses (informational only) ── #}
  {% if inv.expenses %}
  <div style="margin-bottom:1.5rem;padding-bottom:1.5rem;border-bottom:1px solid var(--border);">
    <div class="field-label" style="margin-bottom:.6rem;">Expenses <span style="text-transform:none;font-size:.65rem;">(not included in Amount)</span></div>
    <ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.4rem;font-size:.82rem;">
      {% for e in inv.expenses|from_json %}
      <li><strong>{{ e.type }}</strong>{% if e.note %} — {{ e.note }}{% endif %}</li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}
```

This needs a `from_json` Jinja filter. Add it in `main.py` next to the existing `hours_minutes` filter (found at `main.py:112`):

```python
@app.template_filter("from_json")
def _from_json(value):
    import json
    if not value:
        return []
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return []
```

- [ ] **Step 6: Add a "Total Hours" field (closes doc item 1.2 — "sum all hours")**

The existing "Amount" field already IS the sum of all dollars (Task 4 computes it that way), but there's no display of total hours across the primary line plus all extra lines. In `templates/portal_invoice.html`, replace the existing `Amount` block (lines 39-42):

```html
    <div>
      <div class="field-label">Amount</div>
      <div style="font-size:1.5rem;font-weight:600;">${{ "%.2f"|format(inv.amount) }}</div>
    </div>
```

with:

```html
    <div>
      <div class="field-label">Amount</div>
      <div style="font-size:1.5rem;font-weight:600;">${{ "%.2f"|format(inv.amount) }}</div>
    </div>
    {% set extra_lines = inv.interpreter_rates|from_json %}
    {% set total_hrs = (inv.duration_hours or 0) + extra_lines|sum(attribute='duration') %}
    {% if extra_lines %}
    <div><div class="field-label">Total Hours</div><div style="font-size:.88rem;">{{ total_hrs|hours_minutes }}</div></div>
    {% endif %}
```

(Only shown when there are extra lines — for a single-band invoice, `Duration` already shows the total, so a duplicate `Total Hours` would be redundant. Relies on the `from_json` filter added in Step 5.)

- [ ] **Step 7: Run tests, verify pass**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -q`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add portal_admin.py main.py templates/portal_admin_invoice_create.html templates/portal_invoice.html tests/test_phase9_invoice_lifecycle.py
git commit -m "feat(portal): add informational Expenses section to interpreter invoices"
```

---

### Task 6: Edit Invoice route (locked once submitted)

**Files:**
- Modify: `portal_pages.py` (new route, near `invoice_detail` at line 942)
- Modify: `templates/portal_invoice.html` (add Edit button)
- Create: `templates/portal_invoice_edit.html`
- Test: `tests/test_phase9_invoice_lifecycle.py` (append)

**Interfaces:**
- Produces: `GET/POST /portal/invoices/<int:invoice_id>/edit`. GET renders a form pre-filled with the invoice's current description/notes/expenses (differential lines and amount are NOT editable here — they were computed at creation time; editing them would desync from the real job). POST updates `description`, `notes` (admin only), and `expenses`. 403s (via `abort(403)`) if `inv.submitted` is `TRUE` or the invoice doesn't belong to the requester.

- [ ] **Step 1: Write the failing test**

```python
# ── 6. Edit Invoice (locked once submitted) ─────────────────────────────────

def test_edit_invoice_updates_description_and_expenses(app, world):
    c = _client(app, INTERP_EMAIL)
    row = portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, description) "
        "VALUES (%s, 100, 'unpaid', 'old') RETURNING id", (world["interp"],))
    iid = row["id"]
    r = c.post(f"/portal/invoices/{iid}/edit", data={
        "csrf_token": _csrf(c), "description": "new description",
        "expense_type_0": "Parking", "expense_note_0": "garage",
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    inv = portal_db.query_one("SELECT * FROM invoices WHERE id = %s", (iid,))
    assert inv["description"] == "new description"
    assert json.loads(inv["expenses"]) == [{"type": "Parking", "note": "garage"}]


def test_edit_invoice_blocked_once_submitted(app, world):
    c = _client(app, INTERP_EMAIL)
    row = portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, submitted, description) "
        "VALUES (%s, 100, 'unpaid', TRUE, 'old') RETURNING id", (world["interp"],))
    iid = row["id"]
    r = c.get(f"/portal/invoices/{iid}/edit")
    assert r.status_code == 403
    r2 = c.post(f"/portal/invoices/{iid}/edit", data={
        "csrf_token": _csrf(c), "description": "hacked",
    }, follow_redirects=False)
    assert r2.status_code == 403
    inv = portal_db.query_one("SELECT description FROM invoices WHERE id = %s", (iid,))
    assert inv["description"] == "old"


def test_edit_invoice_blocked_for_someone_elses_invoice(app, world):
    c = _client(app, INTERP2_EMAIL)
    row = portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status) VALUES (%s, 100, 'unpaid') RETURNING id",
        (world["interp"],))
    iid = row["id"]
    assert c.get(f"/portal/invoices/{iid}/edit").status_code == 403
```

- [ ] **Step 2: Run to verify failure**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -k edit_invoice -q`
Expected: FAIL — 404 (route doesn't exist)

- [ ] **Step 3: Add the route**

In `portal_pages.py`, after the `invoice_detail` function (ends at line 978), add:

```python
@pages_bp.route("/portal/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit_invoice(invoice_id):
    """Pre-submission edits only: description, admin notes, expenses. The
    computed rate/differential lines and amount are never editable here —
    they're derived from the linked job at creation time (Task 4); allowing
    ad-hoc edits would silently desync the invoice from the real assignment."""
    role = g.user["role"]
    uid  = g.user["sub"]
    if role == "admin":
        company = g.user["company"]
        inv = portal_db.query_one(
            "SELECT i.* FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "WHERE i.id = %s AND u.company = %s", (invoice_id, company))
    else:
        inv = portal_db.query_one(
            "SELECT * FROM invoices WHERE id = %s AND user_id = %s", (invoice_id, uid))
    if not inv:
        abort(404)
    if inv.get("submitted"):
        abort(403)

    if request.method == "GET":
        return render_template("portal_invoice_edit.html", inv=inv)

    description = (request.form.get("description") or "").strip()
    expenses = []
    idx = 0
    while True:
        etype = (request.form.get(f"expense_type_{idx}") or "").strip()
        if not etype and f"expense_type_{idx}" not in request.form:
            break
        enote = (request.form.get(f"expense_note_{idx}") or "").strip()
        if etype:
            expenses.append({"type": etype, "note": enote})
        idx += 1
    expenses_json = json.dumps(expenses) if expenses else None

    if role == "admin":
        notes = (request.form.get("notes") or "").strip()
        portal_db.execute(
            "UPDATE invoices SET description = %s, notes = %s, expenses = %s WHERE id = %s",
            (description or None, notes or None, expenses_json, invoice_id))
    else:
        portal_db.execute(
            "UPDATE invoices SET description = %s, expenses = %s WHERE id = %s",
            (description or None, expenses_json, invoice_id))
    audit_log("invoice_edit", target=f"invoice:{invoice_id}")
    flash("Invoice updated.", "success")
    return redirect(f"/portal/invoices/{invoice_id}")
```

Check `portal_pages.py`'s existing imports at the top of the file for `json` (grep first) — add `import json` if not already present.

- [ ] **Step 4: Create the edit template**

```html
{% extends "portal_base.html" %}
{% block title %}Edit Invoice #{{ inv.id }} — Portal{% endblock %}

{% block content %}
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2rem;">
  <h1>Edit Invoice #{{ inv.id }}</h1>
  <a href="/portal/invoices/{{ inv.id }}" style="font-size:.78rem;color:var(--muted);">← Back to Invoice</a>
</div>

<form method="POST" style="max-width:520px;display:flex;flex-direction:column;gap:1.1rem;">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>

  <label style="display:flex;flex-direction:column;gap:.35rem;font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);">
    Job Notes
    <textarea name="description" rows="3" maxlength="1000"
              style="background:var(--bg);border:1px solid var(--border);color:var(--text);
                     font-family:var(--font);padding:.5rem .75rem;border-radius:var(--radius);
                     font-size:13px;resize:vertical;">{{ inv.description or '' }}</textarea>
  </label>

  {% if g.user.role == 'admin' %}
  <label style="display:flex;flex-direction:column;gap:.35rem;font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);">
    Notes <span style="text-transform:none;font-size:.65rem;">(admin only)</span>
    <textarea name="notes" rows="2" maxlength="1000"
              style="background:var(--bg);border:1px solid var(--border);color:var(--text);
                     font-family:var(--font);padding:.5rem .75rem;border-radius:var(--radius);
                     font-size:13px;resize:vertical;">{{ inv.notes or '' }}</textarea>
  </label>
  {% endif %}

  <div id="expenses"></div>
  <button type="button" id="add-expense-btn"
          style="align-self:flex-start;background:none;border:1px dashed var(--border);color:var(--muted);
                 font-family:var(--font);padding:.4rem .85rem;border-radius:var(--radius);cursor:pointer;font-size:.78rem;">
    + Add Expense
  </button>

  <div style="display:flex;gap:.75rem;align-items:center;">
    <button type="submit" class="btn btn-primary">Save Changes</button>
    <a href="/portal/invoices/{{ inv.id }}" class="btn" style="text-decoration:none;">Cancel</a>
  </div>
</form>

{% block scripts %}
<script id="existing-expenses-data" type="application/json">{{ inv.expenses or '[]' }}</script>
<script nonce="{{ csp_nonce() }}">
(function () {
  var expensesDiv = document.getElementById('expenses');
  var addExpenseBtn = document.getElementById('add-expense-btn');
  var EXPENSE_TYPES = ['Parking', 'Mileage', 'Travel Time', 'Other'];
  function reindexExpenses() {
    var rows = expensesDiv.querySelectorAll('.expense-row');
    for (var i = 0; i < rows.length; i++) {
      rows[i].querySelector('.expense-type').name = 'expense_type_' + i;
      rows[i].querySelector('.expense-note').name = 'expense_note_' + i;
    }
  }
  function addRow(type, note) {
    var idx = expensesDiv.querySelectorAll('.expense-row').length;
    var row = document.createElement('div');
    row.className = 'expense-row';
    row.style.cssText = 'display:grid;grid-template-columns:1fr 2fr auto;gap:.5rem;align-items:end;margin-top:.5rem;';

    var typeSel = document.createElement('select');
    typeSel.className = 'expense-type'; typeSel.name = 'expense_type_' + idx;
    typeSel.style.cssText = 'background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--font);padding:.4rem .6rem;border-radius:var(--radius);font-size:13px;';
    EXPENSE_TYPES.forEach(function (t) {
      var opt = document.createElement('option'); opt.value = t; opt.textContent = t;
      if (t === type) opt.selected = true;
      typeSel.appendChild(opt);
    });

    var noteIn = document.createElement('input');
    noteIn.type = 'text'; noteIn.className = 'expense-note'; noteIn.name = 'expense_note_' + idx;
    noteIn.value = note || ''; noteIn.maxLength = 300;
    noteIn.style.cssText = 'background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--font);padding:.4rem .6rem;border-radius:var(--radius);font-size:13px;';

    var rmBtn = document.createElement('button');
    rmBtn.type = 'button'; rmBtn.textContent = '✕';
    rmBtn.style.cssText = 'background:none;border:none;color:var(--muted);cursor:pointer;font-size:.9rem;padding:.4rem;';
    rmBtn.addEventListener('click', function () { row.remove(); reindexExpenses(); });

    row.appendChild(typeSel); row.appendChild(noteIn); row.appendChild(rmBtn);
    expensesDiv.appendChild(row);
  }
  if (addExpenseBtn) addExpenseBtn.addEventListener('click', function () { addRow('', ''); });
  try {
    JSON.parse(document.getElementById('existing-expenses-data').textContent)
      .forEach(function (e) { addRow(e.type, e.note); });
  } catch (e) {}
})();
</script>
{% endblock %}
{% endblock %}
```

- [ ] **Step 5: Add the Edit button on the detail page**

In `templates/portal_invoice.html`, inside the "not submitted" block (line 153-159), add an Edit link alongside the Submit form:

```html
  {% if g.user.role in ['employee'] and not inv.submitted %}
  <div style="padding-top:1.5rem;border-top:1px solid var(--border);display:flex;gap:.75rem;">
    <a href="/portal/invoices/{{ inv.id }}/edit" class="btn" style="text-decoration:none;">Edit Invoice</a>
    <form method="POST" action="/portal/invoices/{{ inv.id }}/submit">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
      <button type="submit" class="btn btn-primary">Submit Invoice for Approval</button>
    </form>
  </div>
  {% elif inv.submitted and g.user.role == 'employee' %}
```

(This replaces the opening `<div>` of that existing block, keeping the same `{% elif %}` that follows unchanged.)

- [ ] **Step 6: Run tests, verify pass**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add portal_pages.py templates/portal_invoice_edit.html templates/portal_invoice.html tests/test_phase9_invoice_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(portal): Edit Invoice route, locked once submitted

Pre-submission edits cover description, admin notes, and expenses only —
computed rate/differential lines stay derived from the linked job.
EOF
)"
```

---

### Task 7: List page split — draft (editable) vs submitted (read-only)

**Files:**
- Modify: `portal_pages.py:889-916` (`invoices` route)
- Modify: `templates/portal_invoices.html`
- Test: `tests/test_phase9_invoice_lifecycle.py` (append)

**Interfaces:**
- Produces: for `role == 'employee'`, the `invoices` route now passes `draft_invoices` (submitted = FALSE) and `submitted_invoices` (submitted = TRUE) instead of a single `invoices` list; template renders two sections. Admin's view is unchanged (single flat table — admin already has the separate Interpreter Review page for submitted oversight).

- [ ] **Step 1: Write the failing test**

```python
# ── 7. List page split for the employee view ────────────────────────────────

def test_invoices_list_splits_draft_and_submitted(app, world):
    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, submitted) VALUES (%s, 100, 'unpaid', FALSE)",
        (world["interp"],))
    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, submitted) VALUES (%s, 200, 'submitted_for_payment', TRUE)",
        (world["interp"],))
    c = _client(app, INTERP_EMAIL)
    html = c.get("/portal/invoices").get_data(as_text=True)
    assert "Not Yet Submitted" in html
    assert "Submitted" in html
    draft_pos = html.index("$100.00")
    submitted_pos = html.index("$200.00")
    not_yet_header = html.index("Not Yet Submitted")
    submitted_header = html.index("Submitted", not_yet_header + len("Not Yet Submitted"))
    assert not_yet_header < draft_pos < submitted_header < submitted_pos
```

- [ ] **Step 2: Run to verify failure**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -k splits_draft -q`
Expected: FAIL — `"Not Yet Submitted"` not in html

- [ ] **Step 3: Modify the route**

In `portal_pages.py`, replace the `else:` branch of the `invoices()` function (lines 909-915):

```python
    else:
        rows = portal_db.query_all(
            "SELECT id, amount, status, due_date, created_at, submitted "
            "FROM invoices WHERE user_id = %s ORDER BY created_at DESC",
            (uid,),
        )
        interpreters = []
    return render_template("portal_invoices.html", invoices=rows, interpreters=interpreters)
```

with:

```python
    else:
        draft_invoices = portal_db.query_all(
            "SELECT id, amount, status, due_date, created_at, submitted "
            "FROM invoices WHERE user_id = %s AND submitted = FALSE ORDER BY created_at DESC",
            (uid,),
        )
        submitted_invoices = portal_db.query_all(
            "SELECT id, amount, status, due_date, created_at, submitted "
            "FROM invoices WHERE user_id = %s AND submitted = TRUE "
            "ORDER BY submitted_at DESC NULLS LAST, created_at DESC",
            (uid,),
        )
        return render_template("portal_invoices.html", draft_invoices=draft_invoices,
                               submitted_invoices=submitted_invoices, interpreters=[])
    return render_template("portal_invoices.html", invoices=rows, interpreters=interpreters)
```

- [ ] **Step 4: Rewrite the template**

Replace `templates/portal_invoices.html` entirely:

```html
{% extends "portal_base.html" %}
{% block title %}Invoices — Portal{% endblock %}

{% block content %}
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2rem;">
  <h1>Invoices</h1>
  <a href="/portal/admin/invoices/create" class="btn btn-primary">+ New Invoice</a>
</div>

{% macro invoice_table(rows, show_client, checkable) %}
<table class="portal-table">
  <thead>
    <tr>
      {% if checkable %}<th style="width:2rem;"></th>{% endif %}
      <th>#</th>
      {% if show_client %}<th>Client</th>{% endif %}
      <th>Amount</th>
      <th>Status</th>
      <th>Due</th>
      <th>Created</th>
    </tr>
  </thead>
  <tbody>
    {% for inv in rows %}
    <tr {% if not checkable %}style="cursor:pointer;" data-href="/portal/invoices/{{ inv.id }}"{% endif %}>
      {% if checkable %}
      <td onclick="event.stopPropagation();">
        <input type="checkbox" class="invoice-check" form="bulk-submit-form" name="invoice_ids" value="{{ inv.id }}"/>
      </td>
      {% endif %}
      <td style="color:var(--muted);"><a href="/portal/invoices/{{ inv.id }}" style="color:inherit;">#{{ inv.id }}</a></td>
      {% if show_client %}
      <td>{{ inv.full_name or inv.email or '—' }}</td>
      {% endif %}
      <td>${{ "%.2f"|format(inv.amount) }}</td>
      <td><span class="badge badge-{{ inv.status }}">{{ inv.status }}</span></td>
      <td>{{ inv.due_date.strftime('%b %d, %Y') if inv.due_date else '—' }}</td>
      <td style="color:var(--muted);">{{ inv.created_at.strftime('%b %d, %Y') }}</td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% endmacro %}

{% if g.user.role == 'admin' %}
  {% if invoices %}
    {{ invoice_table(invoices, true, false) }}
  {% else %}
  <p style="color:var(--muted);font-size:.78rem;">No invoices yet.</p>
  {% endif %}
{% else %}
  <form method="POST" action="/portal/invoices/bulk-submit" id="bulk-submit-form"></form>

  <h2 style="font-size:.95rem;margin-bottom:.75rem;">Not Yet Submitted</h2>
  {% if draft_invoices %}
    {{ invoice_table(draft_invoices, false, true) }}
    <div style="margin:1rem 0 2rem;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}" form="bulk-submit-form"/>
      <button type="submit" class="btn btn-primary" form="bulk-submit-form">Submit Selected</button>
    </div>
  {% else %}
  <p style="color:var(--muted);font-size:.78rem;margin-bottom:2rem;">Nothing to submit.</p>
  {% endif %}

  <h2 style="font-size:.95rem;margin-bottom:.75rem;">Submitted</h2>
  {% if submitted_invoices %}
    {{ invoice_table(submitted_invoices, false, false) }}
  {% else %}
  <p style="color:var(--muted);font-size:.78rem;">Nothing submitted yet.</p>
  {% endif %}
{% endif %}
{% endblock %}
```

(Admin's branch reuses `invoices` — restore that variable name in the `if` branch of the route for admins, unchanged from today.)

- [ ] **Step 5: Run tests, verify pass**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -q`
Expected: all pass except the bulk-submit route test (Task 8 implements it) — the split test itself should pass now

- [ ] **Step 6: Commit**

```bash
git add portal_pages.py templates/portal_invoices.html tests/test_phase9_invoice_lifecycle.py
git commit -m "feat(portal): split interpreter invoice list into draft/submitted sections"
```

---

### Task 8: Bulk submit + submission email notification

**Files:**
- Modify: `portal_pages.py` (new route `bulk_submit_invoices`; extend existing `submit_invoice`)
- Test: `tests/test_phase9_invoice_lifecycle.py` (append)

**Interfaces:**
- Consumes: `portal_email.coordinator_recipients`, `portal_email.send_master_invoice_email` (both existing, used the same way `portal_interpreter_invoices._notify_admins` uses them).
- Produces: `POST /portal/invoices/bulk-submit` — accepts `invoice_ids` (multi-value form field), sets `submitted = TRUE, submitted_at = NOW()` on every one that belongs to the requester and isn't already submitted, in one transaction, then emails coordinators once per invoice (reusing the existing template function). Also adds the same email to the single-invoice `submit_invoice` route (item 1.4's "notifies admin ... email + appears there" — "appears there" already works today since `interpreter_review()` reads any `submitted = TRUE` employee invoice; only the email was missing).

- [ ] **Step 1: Write the failing tests**

```python
# ── 8. Bulk submit + notification ───────────────────────────────────────────

def test_bulk_submit_marks_only_owned_unsubmitted_invoices(app, world):
    mine_draft = portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status) VALUES (%s, 100, 'unpaid') RETURNING id",
        (world["interp"],))["id"]
    mine_already = portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, submitted) VALUES (%s, 50, 'unpaid', TRUE) RETURNING id",
        (world["interp"],))["id"]
    someone_elses = portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status) VALUES (%s, 75, 'unpaid') RETURNING id",
        (world["interp2"],))["id"]

    c = _client(app, INTERP_EMAIL)
    r = c.post("/portal/invoices/bulk-submit", data={
        "csrf_token": _csrf(c),
        "invoice_ids": [str(mine_draft), str(mine_already), str(someone_elses)],
    }, follow_redirects=False)
    assert r.status_code == 302, r.data

    assert portal_db.query_one(
        "SELECT submitted FROM invoices WHERE id = %s", (mine_draft,))["submitted"] is True
    assert portal_db.query_one(
        "SELECT submitted FROM invoices WHERE id = %s", (someone_elses,))["submitted"] is False


def test_bulk_submit_requires_at_least_one_selection(app, world):
    c = _client(app, INTERP_EMAIL)
    r = c.post("/portal/invoices/bulk-submit", data={"csrf_token": _csrf(c)},
               follow_redirects=False)
    assert r.status_code == 302  # redirects back with a flash, no crash


def test_submit_invoice_notifies_coordinators(app, world, monkeypatch):
    sent = []
    import portal_pages
    monkeypatch.setattr(portal_pages.portal_email, "send_master_invoice_email",
                        lambda *a, **k: sent.append(a))
    monkeypatch.setattr(portal_pages.portal_email, "coordinator_recipients",
                        lambda company: ["coordinator@example.test"])
    iid = portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status) VALUES (%s, 100, 'unpaid') RETURNING id",
        (world["interp"],))["id"]
    c = _client(app, INTERP_EMAIL)
    r = c.post(f"/portal/invoices/{iid}/submit", data={"csrf_token": _csrf(c)},
               follow_redirects=False)
    assert r.status_code == 302
    assert len(sent) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -k "bulk_submit or notifies_coordinators" -q`
Expected: FAIL — 404 on bulk-submit; `sent` stays empty on the notify test

- [ ] **Step 3: Check `portal_pages.py`'s existing import of `portal_email`**

Run: `grep -n "^import portal_email\|^from portal_email" portal_pages.py`
If it's not imported as `portal_email` (module, not `from portal_email import X`), add `import portal_email` near the top alongside the other `portal_*` imports — the test above patches `portal_pages.portal_email.send_master_invoice_email`, which requires the module-level import form.

- [ ] **Step 4: Add the bulk-submit route and extend `submit_invoice`**

Add after `edit_invoice` (Task 6):

```python
@pages_bp.route("/portal/invoices/bulk-submit", methods=["POST"])
@login_required
def bulk_submit_invoices():
    if g.user["role"] != "employee":
        abort(403)
    uid = g.user["sub"]
    raw_ids = request.form.getlist("invoice_ids")
    ids = []
    for r in raw_ids:
        try:
            ids.append(int(r))
        except (TypeError, ValueError):
            continue
    if not ids:
        flash("Select at least one invoice to submit.", "error")
        return redirect("/portal/invoices")

    with portal_db.transaction() as cur:
        cur.execute(
            "UPDATE invoices SET submitted = TRUE, submitted_at = NOW() "
            "WHERE id = ANY(%s) AND user_id = %s AND submitted = FALSE "
            "RETURNING id, amount",
            (ids, uid),
        )
        updated = cur.fetchall()

    if not updated:
        flash("No eligible invoices were submitted.", "error")
        return redirect("/portal/invoices")

    audit_log("invoice_bulk_submit", target=f"user:{uid}",
              metadata={"invoice_ids": [row[0] for row in updated]})
    try:
        company = g.user["company"]
        recipients = portal_email.coordinator_recipients(company)
        company_name = {"rosesli": "Rose Sign Language Interpreting",
                        "dod": "DOD Cyber Consulting"}.get(company, company)
        interp_name = g.user.get("name") or g.user.get("email") or "An interpreter"
        for inv_id, amount in updated:
            link = request.url_root.rstrip("/") + f"/portal/invoices/{inv_id}"
            for email in recipients:
                portal_email.send_master_invoice_email(
                    email, interp_name, inv_id, float(amount or 0), [], link, company_name)
    except Exception:
        pass  # email failure must not lose the submitted invoices

    flash(f"{len(updated)} invoice(s) submitted for approval.", "success")
    return redirect("/portal/invoices")
```

Modify the existing `submit_invoice` route (`portal_pages.py:1101-1128`) — after the `portal_db.execute("UPDATE invoices SET submitted = TRUE ...")` call and before `flash(...)`, insert:

```python
    try:
        row = portal_db.query_one(
            "SELECT i.amount, u.company, u.full_name, u.email "
            "FROM invoices i JOIN portal_users u ON u.id = i.user_id WHERE i.id = %s",
            (invoice_id,))
        if row:
            recipients = portal_email.coordinator_recipients(row["company"])
            company_name = {"rosesli": "Rose Sign Language Interpreting",
                            "dod": "DOD Cyber Consulting"}.get(row["company"], row["company"])
            link = request.url_root.rstrip("/") + f"/portal/invoices/{invoice_id}"
            for email in recipients:
                portal_email.send_master_invoice_email(
                    email, row.get("full_name") or row.get("email") or "An interpreter",
                    invoice_id, float(row["amount"] or 0), [], link, company_name)
    except Exception:
        pass  # email failure must not block the submit
```

- [ ] **Step 5: Run tests, verify pass**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/test_phase9_invoice_lifecycle.py -q`
Expected: all pass

- [ ] **Step 6: Run the full suite**

Run: `DB_HOST=localhost DB_NAME=botdb DB_USER=botuser DB_PASS=password .venv/Scripts/python -m pytest tests/ -q`
Expected: same 3 pre-existing failures, nothing new

- [ ] **Step 7: Commit**

```bash
git add portal_pages.py tests/test_phase9_invoice_lifecycle.py
git commit -m "$(cat <<'EOF'
feat(portal): bulk-submit multiple invoices + email admin on submit

Checkboxes on the draft section of /portal/invoices submit several
invoices in one transaction. Both the single-submit and bulk-submit paths
now email coordinators (interpreter_review already showed submitted
invoices; only the email notification was missing).
EOF
)"
```

---

### Task 9: Update the batch-tracking doc

**Files:**
- Modify: `docs/portal-feature-batch-2026-07-22.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Flip Phase 1's status column**

In `docs/portal-feature-batch-2026-07-22.md`, change every `⬜` in the "Phase 1" table (rows 1.1 through 1.8) to `✅`.

- [ ] **Step 2: Append to the Progress log**

```markdown
- 2026-07-23: Phase 1 shipped (Tasks 1-8 of `docs/superpowers/plans/2026-07-23-portal-phase1-invoice-lifecycle.md`).
  Job-linked invoices with server-computed, proportionally-split time-band
  differentials (day/evening/overnight x weekday/weekend); 2-hour minimum
  billing preserved. Expenses are informational only (type + note, no
  dollar amount — confirmed with Charles 2026-07-23, deviates from a naive
  literal reading of "sum all dollars" in 1.2 since expenses were never
  specified to carry an amount). Edit Invoice locks once submitted. Draft
  vs Submitted split + bulk-submit shipped on the existing `/portal/invoices`
  employee view; admin's view unchanged (still flat, still has Interpreter
  Review for oversight). The legacy freeform (no-job) manual invoice
  creation path is untouched and still available to admins for ad-hoc pay
  adjustments — Phase 10 (nav restructure) should revisit whether that's
  still needed once Phase 1-9 are all live.
```

- [ ] **Step 3: Commit**

```bash
git add docs/portal-feature-batch-2026-07-22.md
git commit -m "docs: mark Phase 1 done in the 2026-07-22 batch tracker"
```

---

## Post-plan: deploy

This plan does not include a `gcloud builds submit` / Cloud Run deploy step. Per the batch doc's own precedent (Phase 0's phone-number change was committed/pushed but deploy was called out as needing separate approval), get explicit go-ahead before deploying Phase 1 to production — it touches real payroll math for a live business. Once approved: `gcloud builds submit --config cloudbuild.yaml .` from the repo root (see `cloudbuild.yaml`).
