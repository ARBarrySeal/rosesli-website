"""Phase 9 (2026-07-22 batch) — Job # everywhere.

Clarified with Charles: "Job #" means the existing padded job_number
already shown on the assignments list / offers / dashboards / requests
(e.g. "007") — NOT the raw internal jobs.id. This phase extends that same
job_number to the surfaces that were still missing it: interpreter invoice
list/detail, client invoice list/detail/review, the calendar's fallback
label, and the job-related notification emails.
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
import portal_email
from portal_auth import hash_password

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase9_12345!"

ADMIN_EMAIL = "pytest-p9-admin@example.test"
INT_EMAIL = "pytest-p9-int@example.test"
CLIENT_EMAIL = "pytest-p9-client@example.test"
EMAILS = [ADMIN_EMAIL, INT_EMAIL, CLIENT_EMAIL]

MARKER = "pytest-p9-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=55)


def _cleanup():
    portal_db.execute(
        "DELETE FROM invoices WHERE job_id IN (SELECT id FROM jobs WHERE notes = %s AND company = %s)",
        (MARKER, COMPANY),
    )
    portal_db.execute(
        "DELETE FROM client_invoices WHERE job_id IN (SELECT id FROM jobs WHERE notes = %s AND company = %s)",
        (MARKER, COMPANY),
    )
    portal_db.execute("DELETE FROM jobs WHERE notes = %s AND company = %s", (MARKER, COMPANY))
    portal_db.execute("DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY))
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))


def _mk_user(email, role, **cols):
    base = "INSERT INTO portal_users (email, password_hash, full_name, role, company, active"
    vals = [email, hash_password(PW), cols.pop("full_name", f"Pytest {role.title()}"), role, COMPANY, True]
    extra = ""
    for k, v in cols.items():
        extra += f", {k}"
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    row = portal_db.execute(f"{base}{extra}) VALUES ({placeholders}) RETURNING id", tuple(vals))
    return row["id"]


@pytest.fixture
def world():
    _cleanup()
    admin_id = _mk_user(ADMIN_EMAIL, "admin")
    int_id = _mk_user(INT_EMAIL, "employee")
    client_id = _mk_user(CLIENT_EMAIL, "client", full_name="Acme Corp")
    job = portal_db.execute(
        "INSERT INTO jobs (company, status, event_date, notes, job_number, client_id, client_name) "
        "VALUES (%s, 'confirmed', %s, %s, "
        "  (SELECT COALESCE(MAX(job_number), 0) + 1 FROM jobs WHERE company = %s), %s, %s) "
        "RETURNING id, job_number",
        (COMPANY, EVENT_DATE, MARKER, COMPANY, client_id, "Acme Corp"),
    )
    yield {"admin": admin_id, "int": int_id, "client": client_id,
           "job": job["id"], "job_number": job["job_number"]}
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


def _job_num_str(world):
    return f"{world['job_number']:03d}"


# ── 1. Interpreter invoices: list + detail ──────────────────────────────────

def test_interpreter_invoice_list_shows_job_number(app, world):
    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, job_id) VALUES (%s, %s, %s)",
        (world["int"], 100.0, world["job"]),
    )
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal/invoices").data.decode()
    assert _job_num_str(world) in html


def test_interpreter_invoice_detail_shows_job_number(app, world):
    row = portal_db.execute(
        "INSERT INTO invoices (user_id, amount, job_id) VALUES (%s, %s, %s) RETURNING id",
        (world["int"], 100.0, world["job"]),
    )
    intp = _client(app, INT_EMAIL)
    html = intp.get(f"/portal/invoices/{row['id']}").data.decode()
    assert _job_num_str(world) in html


# ── 2. Client invoices: list + detail + review ──────────────────────────────

def test_client_invoice_list_shows_job_number(app, world):
    portal_db.execute(
        "INSERT INTO client_invoices (company, client_id, job_id, submitted) "
        "VALUES (%s, %s, %s, TRUE)",
        (COMPANY, world["client"], world["job"]),
    )
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal/client-invoices").data.decode()
    assert _job_num_str(world) in html


def test_client_invoice_detail_shows_job_number(app, world):
    row = portal_db.execute(
        "INSERT INTO client_invoices (company, client_id, job_id, submitted) "
        "VALUES (%s, %s, %s, TRUE) RETURNING id",
        (COMPANY, world["client"], world["job"]),
    )
    client_user = _client(app, CLIENT_EMAIL)
    html = client_user.get(f"/portal/client-invoices/{row['id']}").data.decode()
    assert _job_num_str(world) in html


def test_client_review_shows_job_number(app, world):
    portal_db.execute(
        "INSERT INTO client_invoices (company, client_id, job_id, submitted) "
        "VALUES (%s, %s, %s, FALSE)",
        (COMPANY, world["client"], world["job"]),
    )
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal/admin/client-review").data.decode()
    assert _job_num_str(world) in html


# ── 3. Calendar shows job_number, never the raw internal id ────────────────

def test_calendar_shows_job_number_not_raw_id(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get(f"/portal/calendar?year={EVENT_DATE.year}&month={EVENT_DATE.month}").data.decode()
    assert _job_num_str(world) in html
    assert f"#{world['job']}" not in html


# ── 4. Emails carry Job # ───────────────────────────────────────────────────

def test_job_when_includes_job_number(world):
    job = portal_db.query_one("SELECT * FROM jobs WHERE id = %s", (world["job"],))
    line = portal_email._job_when(job)
    assert f"Job #{_job_num_str(world)}" in line


def test_job_when_zip_only_includes_job_number(world):
    job = portal_db.query_one("SELECT * FROM jobs WHERE id = %s", (world["job"],))
    line = portal_email._job_when_zip_only(job)
    assert f"Job #{_job_num_str(world)}" in line


def test_job_when_gracefully_omits_missing_job_number():
    line = portal_email._job_when({"event_date": None, "start_time": None, "end_time": None})
    assert "Job #" not in line
