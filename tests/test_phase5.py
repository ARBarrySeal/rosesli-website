"""Phase 5 — client invoices + Requests (Rose SLI).

Covers the genuinely-new Phase 5 work:

  * ensure_invoice_for_job: auto-create an unpaid client invoice for a
    *confirmed* job that has a client account + client rate (dedup by job_id;
    skip pending jobs and jobs with no billable rate)
  * create_assignment / edit_assignment fire that auto-create on confirm
  * Create-Client-Invoice form prefills from ?job=<id> and persists job_id
  * /portal/requests lists a client's own jobs (and the public-request inbox
    for admins); employees are forbidden
  * Client sidebar exposes the Requests link
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
from portal_auth import hash_password


COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase5_12345!"

ADMIN_EMAIL = "pytest-p5-admin@example.test"
CLIENT_EMAIL = "pytest-p5-client@example.test"
INT_EMAIL = "pytest-p5-int@example.test"
EMAILS = [ADMIN_EMAIL, CLIENT_EMAIL, INT_EMAIL]

JOB_MARKER = "pytest-p5-job-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=14)


# ── Setup / teardown ──────────────────────────────────────────────────────────

def _cleanup():
    # job_id is ON DELETE SET NULL, so a half-cleaned prior run can leave orphan
    # invoices whose job_id is nulled but still reference the test users via
    # created_by / client_id — sweep those too, or the user delete below FK-fails.
    portal_db.execute(
        "DELETE FROM client_invoices WHERE "
        "job_id IN (SELECT id FROM jobs WHERE (notes = %s OR interpreter_notes = %s) AND company = %s) "
        "OR created_by IN (SELECT id FROM portal_users WHERE email = ANY(%s)) "
        "OR client_id IN (SELECT id FROM portal_users WHERE email = ANY(%s))",
        (JOB_MARKER, JOB_MARKER, COMPANY, EMAILS, EMAILS),
    )
    portal_db.execute("DELETE FROM jobs WHERE (notes = %s OR interpreter_notes = %s) AND company = %s", (JOB_MARKER, JOB_MARKER, COMPANY))
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY),
    )
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))


def _mk_user(email, role, **cols):
    base = "INSERT INTO portal_users (email, password_hash, full_name, role, company, active"
    vals = [email, hash_password(PW), cols.pop("full_name", f"Pytest {role.title()}"), role, COMPANY, True]
    extra = ""
    for k, v in cols.items():
        extra += f", {k}"
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    row = portal_db.execute(
        f"{base}{extra}) VALUES ({placeholders}) RETURNING id", tuple(vals),
    )
    return row["id"]


@pytest.fixture
def world():
    _cleanup()
    admin_id = _mk_user(ADMIN_EMAIL, "admin")
    client_id = _mk_user(CLIENT_EMAIL, "client", full_name="Acme Corp", interpreter_rate=125)
    int_id = _mk_user(INT_EMAIL, "employee", full_name="Casey Terp")
    yield {"admin": admin_id, "client": client_id, "int": int_id}
    _cleanup()


def _mk_job(**cols):
    cols.setdefault("company", COMPANY)
    cols.setdefault("status", "pending")
    cols.setdefault("notes", JOB_MARKER)
    keys = list(cols.keys())
    placeholders = ", ".join(["%s"] * len(keys))
    row = portal_db.execute(
        f"INSERT INTO jobs ({', '.join(keys)}) VALUES ({placeholders}) RETURNING id",
        tuple(cols[k] for k in keys),
    )
    return row["id"]


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _login(client, email):
    with client.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        csrf = sess["csrf_token"]
    r = client.post("/login", data={"email": email, "password": PW, "csrf_token": csrf})
    assert r.status_code == 200, r.data
    return client


def _csrf(client):
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def _client(app, email):
    return _login(app.test_client(), email)


# ── 1. ensure_invoice_for_job ─────────────────────────────────────────────────

def test_ensure_invoice_creates_for_confirmed_billable_job(world):
    from portal_client_invoices import ensure_invoice_for_job
    job = _mk_job(status="confirmed", client_id=world["client"], client_name="Acme Corp",
                  client_rate=150, duration="2", event_date=EVENT_DATE)
    inv_id = ensure_invoice_for_job(COMPANY, job, world["admin"])
    assert inv_id is not None
    inv = portal_db.query_one("SELECT * FROM client_invoices WHERE id = %s", (inv_id,))
    assert inv["job_id"] == job
    assert float(inv["rate_per_hour"]) == 150.0
    assert float(inv["total"]) == 300.0
    assert inv["client_id"] == world["client"]


def test_ensure_invoice_dedups_by_job(world):
    from portal_client_invoices import ensure_invoice_for_job
    job = _mk_job(status="confirmed", client_id=world["client"], client_rate=150, duration="2")
    first = ensure_invoice_for_job(COMPANY, job, world["admin"])
    second = ensure_invoice_for_job(COMPANY, job, world["admin"])
    assert first == second
    n = portal_db.query_one(
        "SELECT COUNT(*) AS n FROM client_invoices WHERE job_id = %s", (job,),
    )["n"]
    assert n == 1


def test_ensure_invoice_skips_pending_job(world):
    from portal_client_invoices import ensure_invoice_for_job
    job = _mk_job(status="pending", client_id=world["client"], client_rate=150)
    assert ensure_invoice_for_job(COMPANY, job, world["admin"]) is None


def test_ensure_invoice_resolves_rate_when_job_has_none(world):
    # Since mig 015 a missing job snapshot rate resolves from the client's
    # rate effective on the service date (falls back to interpreter_rate=125).
    from portal_client_invoices import ensure_invoice_for_job
    job = _mk_job(status="confirmed", client_id=world["client"])
    inv_id = ensure_invoice_for_job(COMPANY, job, world["admin"])
    assert inv_id is not None
    inv = portal_db.query_one(
        "SELECT rate_per_hour FROM client_invoices WHERE id = %s", (inv_id,))
    assert float(inv["rate_per_hour"]) == 125.0


def test_ensure_invoice_skips_when_no_rate_anywhere(world):
    from portal_client_invoices import ensure_invoice_for_job
    portal_db.execute(
        "UPDATE portal_users SET interpreter_rate = NULL WHERE id = %s",
        (world["client"],))
    job = _mk_job(status="confirmed", client_id=world["client"])
    assert ensure_invoice_for_job(COMPANY, job, world["admin"]) is None


def test_ensure_invoice_handles_nonnumeric_duration(world):
    from portal_client_invoices import ensure_invoice_for_job
    job = _mk_job(status="confirmed", client_id=world["client"], client_rate=100,
                  duration="half day")
    inv_id = ensure_invoice_for_job(COMPANY, job, world["admin"])
    assert inv_id is not None
    inv = portal_db.query_one("SELECT total FROM client_invoices WHERE id = %s", (inv_id,))
    assert inv["total"] is None  # can't compute without numeric hours


# ── 2. Auto-create wired into assignment create/edit ──────────────────────────

def test_create_confirmed_assignment_auto_bills(app, world):
    admin = _client(app, ADMIN_EMAIL)
    admin.post("/portal/admin/assignments/new", data={
        "csrf_token": _csrf(admin),
        "status": "confirmed",
        "client_id": world["client"],
        "client_rate": "150",
        "duration": "2",
        "interpreter_notes": JOB_MARKER,
    })
    job = portal_db.query_one(
        "SELECT id FROM jobs WHERE interpreter_notes = %s AND company = %s ORDER BY id DESC",
        (JOB_MARKER, COMPANY),
    )
    inv = portal_db.query_one(
        "SELECT * FROM client_invoices WHERE job_id = %s", (job["id"],),
    )
    assert inv is not None
    assert float(inv["total"]) == 300.0


def test_create_pending_assignment_does_not_bill(app, world):
    admin = _client(app, ADMIN_EMAIL)
    admin.post("/portal/admin/assignments/new", data={
        "csrf_token": _csrf(admin),
        "status": "pending",
        "client_id": world["client"],
        "client_rate": "150",
        "interpreter_notes": JOB_MARKER,
    })
    job = portal_db.query_one(
        "SELECT id FROM jobs WHERE interpreter_notes = %s AND company = %s ORDER BY id DESC",
        (JOB_MARKER, COMPANY),
    )
    inv = portal_db.query_one("SELECT id FROM client_invoices WHERE job_id = %s", (job["id"],))
    assert inv is None


def test_edit_to_confirmed_auto_bills(app, world):
    job = _mk_job(status="pending", client_id=world["client"], client_rate=150, duration="3")
    admin = _client(app, ADMIN_EMAIL)
    admin.post(f"/portal/admin/assignments/{job}/edit", data={
        "csrf_token": _csrf(admin),
        "status": "confirmed",
        "client_id": world["client"],
        "client_rate": "150",
        "duration": "3",
        "interpreter_notes": JOB_MARKER,
    })
    inv = portal_db.query_one("SELECT total FROM client_invoices WHERE job_id = %s", (job,))
    assert inv is not None
    assert float(inv["total"]) == 450.0


# ── 3. Create-invoice prefill from request + job_id persistence ────────────────

def test_create_invoice_prefills_from_job(app, world):
    job = _mk_job(status="pending", client_id=world["client"], client_name="Acme Corp",
                  client_rate=175, poc_email="poc@acme.test", event_date=EVENT_DATE)
    admin = _client(app, ADMIN_EMAIL)
    r = admin.get(f"/portal/admin/client-invoices/create?job={job}")
    assert r.status_code == 200
    body = r.data.decode()
    assert "175" in body              # rate prefilled
    assert "poc@acme.test" in body    # poc prefilled
    assert f'name="job_id" value="{job}"' in body  # link carried as hidden field


def test_create_invoice_persists_job_id(app, world):
    job = _mk_job(status="pending", client_id=world["client"], client_rate=175)
    admin = _client(app, ADMIN_EMAIL)
    admin.post("/portal/admin/client-invoices/create", data={
        "csrf_token": _csrf(admin),
        "client_id": world["client"],
        "rate_per_hour": "175",
        "duration_hours": "2",
        "job_id": str(job),
    })
    inv = portal_db.query_one(
        "SELECT job_id, total FROM client_invoices WHERE job_id = %s", (job,),
    )
    assert inv is not None
    assert inv["job_id"] == job
    assert float(inv["total"]) == 350.0


# ── 4. /portal/requests ────────────────────────────────────────────────────────

def test_requests_page_lists_client_own_jobs(app, world):
    _mk_job(status="pending", client_id=world["client"], setting="Medical Appt")
    cl = _client(app, CLIENT_EMAIL)
    r = cl.get("/portal/requests")
    assert r.status_code == 200
    assert "Medical Appt" in r.data.decode()


def test_requests_page_admin_sees_public_request_inbox(app, world):
    _mk_job(status="pending", source="public_request", requester_name="Walk In Wendy")
    admin = _client(app, ADMIN_EMAIL)
    r = admin.get("/portal/requests")
    assert r.status_code == 200
    assert "Walk In Wendy" in r.data.decode()


def test_requests_page_forbidden_for_employee(app, world):
    emp = _client(app, INT_EMAIL)
    r = emp.get("/portal/requests")
    assert r.status_code == 403


def test_client_sidebar_has_requests_link(app, world):
    cl = _client(app, CLIENT_EMAIL)
    r = cl.get("/portal")
    assert r.status_code == 200
    assert "/portal/requests" in r.data.decode()
