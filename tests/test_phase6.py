"""Phase 6 — interpreter master invoice (Rose SLI).

Covers the genuinely-new Phase 6 work:

  * billable_jobs_for_interpreter: confirmed/completed jobs for THIS interpreter
    that aren't already on a master invoice, each with a computed line_amount
    (duration x interpreter_rate); pending jobs and other people's jobs excluded
  * create_master_invoice: bundles selected jobs into ONE 'submitted_for_payment'
    invoice + invoice_jobs rows; filters submitted ids against the billable set
    so it can't double-bill or bill someone else's work
  * /portal/pay/review lists the interpreter's billable jobs; employee-only
  * /portal/pay/submit creates the master invoice and redirects to its detail
  * the interpreter sidebar exposes the Submit-for-Payment link
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
from portal_auth import hash_password


COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase6_12345!"

ADMIN_EMAIL = "pytest-p6-admin@example.test"
INT_EMAIL = "pytest-p6-int@example.test"
OTHER_EMAIL = "pytest-p6-other@example.test"
EMAILS = [ADMIN_EMAIL, INT_EMAIL, OTHER_EMAIL]

JOB_MARKER = "pytest-p6-job-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=7)


# ── Setup / teardown ──────────────────────────────────────────────────────────

def _cleanup():
    # invoice_jobs.job_id is ON DELETE SET NULL and invoices may have been
    # created for the test interpreter — sweep both before deleting users/jobs.
    portal_db.execute(
        "DELETE FROM invoice_jobs WHERE "
        "job_id IN (SELECT id FROM jobs WHERE notes = %s AND company = %s) "
        "OR invoice_id IN (SELECT id FROM invoices WHERE user_id IN "
        "  (SELECT id FROM portal_users WHERE email = ANY(%s)))",
        (JOB_MARKER, COMPANY, EMAILS),
    )
    portal_db.execute(
        "DELETE FROM invoices WHERE user_id IN "
        "(SELECT id FROM portal_users WHERE email = ANY(%s))",
        (EMAILS,),
    )
    portal_db.execute("DELETE FROM jobs WHERE notes = %s AND company = %s", (JOB_MARKER, COMPANY))
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
    int_id = _mk_user(INT_EMAIL, "employee", full_name="Casey Terp", interpreter_rate=80)
    other_id = _mk_user(OTHER_EMAIL, "employee", full_name="Other Terp", interpreter_rate=90)
    yield {"admin": admin_id, "int": int_id, "other": other_id}
    _cleanup()


def _mk_job(**cols):
    cols.setdefault("company", COMPANY)
    cols.setdefault("status", "confirmed")
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


# ── 1. billable_jobs_for_interpreter ───────────────────────────────────────────

def test_billable_lists_confirmed_with_computed_amount(world):
    from portal_interpreter_invoices import billable_jobs_for_interpreter
    job = _mk_job(status="confirmed", interpreter_1_id=world["int"], duration="3",
                  event_date=EVENT_DATE, setting="Medical")
    rows = billable_jobs_for_interpreter(COMPANY, world["int"])
    ids = {r["id"] for r in rows}
    assert job in ids
    row = next(r for r in rows if r["id"] == job)
    assert row["line_amount"] == 240.0   # 3 hrs x $80


def test_billable_excludes_pending(world):
    from portal_interpreter_invoices import billable_jobs_for_interpreter
    job = _mk_job(status="pending", interpreter_1_id=world["int"], duration="2")
    rows = billable_jobs_for_interpreter(COMPANY, world["int"])
    assert job not in {r["id"] for r in rows}


def test_billable_excludes_other_interpreters_jobs(world):
    from portal_interpreter_invoices import billable_jobs_for_interpreter
    other_job = _mk_job(status="confirmed", interpreter_1_id=world["other"], duration="2")
    rows = billable_jobs_for_interpreter(COMPANY, world["int"])
    assert other_job not in {r["id"] for r in rows}


def test_billable_excludes_already_invoiced(world):
    from portal_interpreter_invoices import billable_jobs_for_interpreter, create_master_invoice
    job = _mk_job(status="completed", interpreter_1_id=world["int"], duration="2")
    create_master_invoice(COMPANY, world["int"], [job])
    rows = billable_jobs_for_interpreter(COMPANY, world["int"])
    assert job not in {r["id"] for r in rows}


# ── 2. create_master_invoice ────────────────────────────────────────────────────

def test_create_master_invoice_bundles_and_totals(world):
    from portal_interpreter_invoices import create_master_invoice
    j1 = _mk_job(status="confirmed", interpreter_1_id=world["int"], duration="2")  # 160
    j2 = _mk_job(status="completed", interpreter_2_id=world["int"], duration="1.5")  # 120
    inv_id = create_master_invoice(COMPANY, world["int"], [j1, j2])
    assert inv_id is not None
    inv = portal_db.query_one("SELECT * FROM invoices WHERE id = %s", (inv_id,))
    assert inv["status"] == "submitted_for_payment"
    assert inv["submitted"] is True
    assert float(inv["amount"]) == 280.0
    n = portal_db.query_one(
        "SELECT COUNT(*) AS n FROM invoice_jobs WHERE invoice_id = %s", (inv_id,),
    )["n"]
    assert n == 2


def test_create_master_invoice_ignores_foreign_job_ids(world):
    from portal_interpreter_invoices import create_master_invoice
    mine = _mk_job(status="confirmed", interpreter_1_id=world["int"], duration="2")  # 160
    theirs = _mk_job(status="confirmed", interpreter_1_id=world["other"], duration="5")
    inv_id = create_master_invoice(COMPANY, world["int"], [mine, theirs])
    assert inv_id is not None
    # only the interpreter's own job should be billed
    rows = portal_db.query_all(
        "SELECT job_id FROM invoice_jobs WHERE invoice_id = %s", (inv_id,),
    )
    billed = {r["job_id"] for r in rows}
    assert billed == {mine}
    inv = portal_db.query_one("SELECT amount FROM invoices WHERE id = %s", (inv_id,))
    assert float(inv["amount"]) == 160.0


def test_create_master_invoice_returns_none_when_nothing_valid(world):
    from portal_interpreter_invoices import create_master_invoice
    theirs = _mk_job(status="confirmed", interpreter_1_id=world["other"], duration="5")
    assert create_master_invoice(COMPANY, world["int"], [theirs]) is None
    assert create_master_invoice(COMPANY, world["int"], []) is None


# ── 3. /portal/pay routes ────────────────────────────────────────────────────────

def test_pay_review_lists_billable_for_interpreter(app, world):
    _mk_job(status="confirmed", interpreter_1_id=world["int"], duration="2", setting="Court Hearing")
    emp = _client(app, INT_EMAIL)
    r = emp.get("/portal/pay/review")
    assert r.status_code == 200
    assert "Court Hearing" in r.data.decode()


def test_pay_review_forbidden_for_admin(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.get("/portal/pay/review")
    assert r.status_code == 403


def test_pay_submit_creates_invoice_and_redirects(app, world):
    job = _mk_job(status="confirmed", interpreter_1_id=world["int"], duration="2")
    emp = _client(app, INT_EMAIL)
    r = emp.post("/portal/pay/submit", data={
        "csrf_token": _csrf(emp),
        "job_ids": [str(job)],
    })
    assert r.status_code == 302
    inv = portal_db.query_one(
        "SELECT id, status FROM invoices WHERE user_id = %s ORDER BY id DESC", (world["int"],),
    )
    assert inv is not None
    assert inv["status"] == "submitted_for_payment"
    assert r.headers["Location"].endswith(f"/portal/invoices/{inv['id']}")


def test_pay_submit_requires_a_selection(app, world):
    emp = _client(app, INT_EMAIL)
    r = emp.post("/portal/pay/submit", data={"csrf_token": _csrf(emp)})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/portal/pay/review")


def test_interpreter_sidebar_has_pay_link(app, world):
    emp = _client(app, INT_EMAIL)
    r = emp.get("/portal")
    assert r.status_code == 200
    assert "/portal/pay/review" in r.data.decode()
