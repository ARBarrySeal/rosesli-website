# Usked-style Scheduler for rosesli.com — Implementation Plan

**Goal:** Replicate the core Usked interpreter-agency workflow inside the existing Rose SLI Flask portal: interpreter availability → coordinator offers a job → interpreters accept/decline → coordinator confirms → completing the job auto-drafts billing. Build on existing patterns; never break the live site; tests as we go.

**Stack already in place** (do not reinvent):
- DB access: `portal_db.query_one / query_all / execute` (psycopg2 pool; `execute` returns the `RETURNING` row or None).
- Migrations: numbered idempotent SQL in `migrations/NNN_*.sql`, applied with `python run_migration.py NNN_name.sql`. Use `IF NOT EXISTS` everywhere.
- Auth: `@login_required`, `@admin_required` from `portal_auth`; `g.user` has `sub` (id), `role` (`admin`/`employee`/`client`), `company` (`rosesli`).
- Audit: `audit_log(action, target=..., metadata=...)` from `portal_audit`.
- Email: `portal_email._send(to, subject, body)` → bool. Add `send_*` helpers alongside existing ones.
- Blueprints registered in `main.py` (lines 48–59). Templates extend `portal_base.html`.
- Tests: pytest, `tests/conftest.py` gives `app` + `client` fixtures against local Postgres (botdb).

**Existing jobs model** (migration 005, `portal_jobs.py`): `jobs` table already has `status` (pending/confirmed/completed/cancelled), `interpreter_1_id/2_id` (FK → portal_users, drives interpreter visibility), `interpreter_1_name/2_name` (display snapshot), `client_rate`, `rate_type`, `event_date`, `start_time`, `end_time`. Interpreter pay invoices = `invoices` table (has `job_id`, `interpreter_rates`); client billing = `client_invoices` table.

**Decisions locked with Charles:**
- Availability model = *block-off-unavailable* (interpreters mark when they CANNOT work).
- Matching = *coordinator-offers* (Amanda pushes offers; interpreters respond).
- Notifications = email **and** in-portal.
- Phase 4 billing = **auto-draft** on completion (Amanda reviews + sends; never auto-sent).
- Bundle Phase 1 + 2, ship first.

---

## Phase 1+2 (bundled): Availability + Offer/Accept-Decline/Confirm

### Task 1 — Migration `009_scheduler_offers.sql`
New file `migrations/009_scheduler_offers.sql`, idempotent:

```sql
-- 009: Interpreter availability blocks + job offers (Usked-style matching).
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS interpreter_unavailability (
    id          SERIAL PRIMARY KEY,
    company     TEXT NOT NULL CHECK (company IN ('dod','rosesli')),
    user_id     INT  NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,              -- = start_date for a single day
    all_day     BOOLEAN NOT NULL DEFAULT TRUE,
    start_time  TEXT,                       -- only when all_day = FALSE
    end_time    TEXT,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_unavail_user ON interpreter_unavailability(user_id);
CREATE INDEX IF NOT EXISTS idx_unavail_dates ON interpreter_unavailability(start_date, end_date);

CREATE TABLE IF NOT EXISTS job_offers (
    id            SERIAL PRIMARY KEY,
    company       TEXT NOT NULL CHECK (company IN ('dod','rosesli')),
    job_id        INT  NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    interpreter_id INT NOT NULL REFERENCES portal_users(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'offered'
                    CHECK (status IN ('offered','accepted','declined','withdrawn')),
    note          TEXT,
    offered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    responded_at  TIMESTAMPTZ,
    UNIQUE (job_id, interpreter_id)
);
CREATE INDEX IF NOT EXISTS idx_offers_job    ON job_offers(job_id);
CREATE INDEX IF NOT EXISTS idx_offers_interp ON job_offers(interpreter_id);
CREATE INDEX IF NOT EXISTS idx_offers_status ON job_offers(status);
```
**Verify:** `python run_migration.py 009_scheduler_offers.sql` prints "Migration complete"; re-run is a no-op.

### Task 2 — `portal_availability.py` (new blueprint)
Interpreter-facing availability CRUD + a helper the offer flow reuses.
- `availability_bp = Blueprint("availability", __name__)`
- `GET /portal/availability` (`@login_required`, employee/admin): list this interpreter's upcoming unavailability blocks; admin can `?user_id=` to view one interpreter.
- `POST /portal/availability/add` (`@login_required`): insert a block (date or range, optional time window, note). Validate dates with the existing `_date_or_none` idiom.
- `POST /portal/availability/<int:block_id>/delete` (`@login_required`): delete own block (or admin any).
- Helper `is_unavailable(user_id, company, event_date) -> bool` and `available_interpreters(company, event_date)` — returns `_interpreters()` list minus anyone with a block covering `event_date`. **Reused by the offer UI in Task 4.**
- `audit_log` on add/delete.
- Template `portal_availability.html` extends base; simple form + list with delete buttons.

### Task 3 — `portal_offers.py` (new blueprint) — coordinator side
- `offers_bp = Blueprint("offers", __name__)`
- `POST /portal/admin/assignments/<int:job_id>/offer` (`@admin_required`): body = list of `interpreter_id`s. For each, upsert a `job_offers` row (`status='offered'`). **Block double-booking:** skip/refuse an interpreter already `confirmed` (jobs.interpreter_1_id/2_id) on another job with the same `event_date`. Send email (`send_offer_email`) + create in-portal notice. `audit_log("job_offer", target=f"job:{id}")`.
- `POST /portal/admin/offers/<int:offer_id>/withdraw` (`@admin_required`): set `status='withdrawn'`.
- `POST /portal/admin/assignments/<int:job_id>/confirm` (`@admin_required`): body = `interpreter_id` (must have an `accepted` offer). Set `jobs.interpreter_1_id` + `interpreter_1_name` snapshot, `jobs.status='confirmed'`; set that offer `accepted` (idempotent), `withdraw` all other open offers for the job. Re-check no double-book on event_date. Email the confirmed interpreter (`send_confirm_email`).
- Surface offer state on `portal_assignment_detail.html` (admin sees per-interpreter offer status; "Offer to…" picker fed by `available_interpreters`).

### Task 4 — Interpreter side of offers
- `GET /portal/offers` (`@login_required`, employee): this interpreter's `offered` (pending) + recent responded offers, with job summary.
- `POST /portal/offers/<int:offer_id>/accept` and `/decline` (`@login_required`): only on own `offered` rows; set status + `responded_at=NOW()`. On accept, email/notify admin (`send_offer_response_email`). Guard: can't accept if already confirmed elsewhere on that date.
- Dashboard badge: pending-offer count on `portal_dashboard.html` for employees.

### Task 5 — Notifications
- In `portal_email.py` add: `send_offer_email`, `send_confirm_email`, `send_offer_response_email` — same `_send(...)` plain-text pattern as `send_invite_email`. Include job date/time/location and a deep link (`APP_URL` + `/portal/offers`).
- In-portal: lightweight `portal_notifications` (small table id/user_id/company/kind/body/link/read/created_at) **or** derive from `job_offers` state to avoid a table. Decision at build time — prefer deriving from `job_offers` for pending offers (no new table) and a dashboard badge; only add a table if a richer feed is needed.

### Task 6 — Wire-up + tests
- Register `availability_bp` and `offers_bp` in `main.py` (mirror lines 48–59).
- Add nav links in `portal_base.html`: "Availability" (employee), "Offers" (employee, with badge).
- Tests in `tests/test_scheduler_offers.py`:
  - availability add/list/delete round-trips and is per-user scoped;
  - offer creation skips a double-booked interpreter;
  - accept/decline updates status + `responded_at`;
  - confirm sets `jobs.status='confirmed'`, snapshots name, withdraws sibling offers;
  - employee cannot act on another interpreter's offer (403).
  Follow `tests/test_portal_security.py` auth-cookie conventions.
- **Verify:** `python -m pytest tests/test_scheduler_offers.py -q` green; full suite still green.

**Phase gate:** demo locally (admin offers → interpreter accepts → admin confirms → job shows confirmed). Stop before commit/push/deploy and report to Charles for approval.

---

## Phase 3: Client self-service requests
Clients log in, submit interpreter requests, track status/who's assigned. Reuses `jobs` (`source='public_request'` already exists) + existing client invoice view.
- `GET/POST /portal/request` (client): form → inserts a `jobs` row (`status='pending'`, requester fields from client profile). Email admin.
- `GET /portal/my-requests` (client): list own requests with status + assigned interpreter name (no rates).
- Admin inbox: pending public_request jobs surfaced on dashboard.
- Tests + phase gate.

## Phase 4: Auto-flow to billing
Marking a job `completed` auto-drafts both invoices from rates on file; Amanda reviews + sends (never auto-sent).
- `POST /portal/admin/assignments/<id>/complete` (`@admin_required`): set `status='completed'`; if no existing draft, create:
  - interpreter pay → `invoices` row (`job_id`, `interpreter_rates`, amount from `interpreter_rate` × duration), `status='unpaid'` draft;
  - client bill → `client_invoices` row from `client_rate` × duration + differentials, draft.
- Idempotent (don't double-create). Show "drafted, review before sending" banner; reuse existing invoice edit/detail.
- Tests + phase gate.

---

## Guardrails (every phase)
- Idempotent migrations only; never destructive on live data.
- Company-scope every query (`company='rosesli'`); never leak across dod/rosesli.
- Snapshot display names on write (existing pattern) so deletes don't blank history.
- Ask before commit / push / deploy — treat as 3 separate steps, report all three (per standing instruction).
- Each phase independently shippable + tested before the next.
