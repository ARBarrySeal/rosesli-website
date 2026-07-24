"""Phase 2 (2026-07-22 batch) — Client Review page.

New client invoices are created as drafts (submitted=FALSE) and are invisible
to the client until an admin reviews and submits them via the new
/portal/admin/client-review page (mirrors Interpreter Review's layout/model).

  * migration 020: client_invoices.submitted / submitted_at
  * ensure_invoice_for_job / create_client_invoice both create drafts
  * clients cannot see or open a draft invoice (list + detail both filtered)
  * /portal/admin/client-review lists only drafts, newest first
  * /portal/admin/client-invoices/submit-batch flips selected drafts visible
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
from portal_auth import hash_password

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase2CR_12345!"

ADMIN_EMAIL = "pytest-p2cr-admin@example.test"
CLIENT_EMAIL = "pytest-p2cr-client@example.test"
CLIENT2_EMAIL = "pytest-p2cr-client2@example.test"
EMPLOYEE_EMAIL = "pytest-p2cr-emp@example.test"
EMAILS = [ADMIN_EMAIL, CLIENT_EMAIL, CLIENT2_EMAIL, EMPLOYEE_EMAIL]

MARKER = "pytest-p2cr-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=14)


def _cleanup():
    portal_db.execute(
        "DELETE FROM client_invoices WHERE notes = %s OR "
        "created_by IN (SELECT id FROM portal_users WHERE email = ANY(%s)) OR "
        "client_id IN (SELECT id FROM portal_users WHERE email = ANY(%s))",
        (MARKER, EMAILS, EMAILS),
    )
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
    client_id = _mk_user(CLIENT_EMAIL, "client", full_name="Acme Corp")
    client2_id = _mk_user(CLIENT2_EMAIL, "client", full_name="Beta LLC")
    emp_id = _mk_user(EMPLOYEE_EMAIL, "employee", full_name="Casey Terp")
    yield {"admin": admin_id, "client": client_id, "client2": client2_id, "emp": emp_id}
    _cleanup()


def _mk_invoice(client_id, submitted, total=300.0):
    row = portal_db.execute(
        "INSERT INTO client_invoices (company, client_id, notes, date_of_service, total, submitted) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (COMPANY, client_id, MARKER, EVENT_DATE, total, submitted),
    )
    return row["id"]


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


# ── 1. New invoices are created as drafts ──────────────────────────────────

def test_ensure_invoice_for_job_creates_draft(world):
    from portal_client_invoices import ensure_invoice_for_job
    job = portal_db.execute(
        "INSERT INTO jobs (company, status, client_id, client_rate, duration, notes) "
        "VALUES (%s, 'confirmed', %s, 150, '2', %s) RETURNING id",
        (COMPANY, world["client"], MARKER),
    )["id"]
    inv_id = ensure_invoice_for_job(COMPANY, job, world["admin"])
    inv = portal_db.query_one("SELECT submitted FROM client_invoices WHERE id = %s", (inv_id,))
    assert inv["submitted"] is False
    portal_db.execute("DELETE FROM jobs WHERE id = %s", (job,))


def test_create_client_invoice_route_creates_draft(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post("/portal/admin/client-invoices/create", data={
        "csrf_token": _csrf(admin), "client_id": str(world["client"]),
        "date_of_service": str(EVENT_DATE), "base_rate": "100", "duration_hours": "2",
        "notes": MARKER,
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    assert r.location.endswith("/portal/admin/client-review")
    inv = portal_db.query_one(
        "SELECT submitted FROM client_invoices WHERE notes = %s ORDER BY id DESC LIMIT 1", (MARKER,))
    assert inv["submitted"] is False


# ── 2. Clients cannot see drafts ───────────────────────────────────────────

def test_client_list_excludes_drafts(app, world):
    open_id = _mk_invoice(world["client"], submitted=True)
    _mk_invoice(world["client"], submitted=False)
    c = _client(app, CLIENT_EMAIL)
    html = c.get("/portal/client-invoices").data.decode()
    assert f"#{open_id}" in html


def test_client_cannot_open_draft_detail(app, world):
    draft_id = _mk_invoice(world["client"], submitted=False)
    c = _client(app, CLIENT_EMAIL)
    r = c.get(f"/portal/client-invoices/{draft_id}")
    assert r.status_code == 404


def test_client_can_open_submitted_detail(app, world):
    inv_id = _mk_invoice(world["client"], submitted=True)
    c = _client(app, CLIENT_EMAIL)
    r = c.get(f"/portal/client-invoices/{inv_id}")
    assert r.status_code == 200


# ── 3. /portal/admin/client-review ─────────────────────────────────────────

def test_client_review_lists_only_drafts(app, world):
    draft_id = _mk_invoice(world["client"], submitted=False)
    sub_id = _mk_invoice(world["client"], submitted=True)
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal/admin/client-review").data.decode()
    assert f"#{draft_id}" in html
    assert f"#{sub_id}" not in html


def test_client_review_forbidden_for_client_and_employee(app, world):
    for email in (CLIENT_EMAIL, EMPLOYEE_EMAIL):
        c = _client(app, email)
        r = c.get("/portal/admin/client-review", follow_redirects=False)
        assert r.status_code == 302
        assert r.location.endswith("/portal")


# ── 4. Batch submit ─────────────────────────────────────────────────────────

def test_submit_batch_flips_selected_drafts_only(app, world):
    d1 = _mk_invoice(world["client"], submitted=False)
    d2 = _mk_invoice(world["client"], submitted=False)
    d3_untouched = _mk_invoice(world["client"], submitted=False)
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post("/portal/admin/client-invoices/submit-batch", data={
        "csrf_token": _csrf(admin), "invoice_ids": [str(d1), str(d2)],
    }, follow_redirects=False)
    assert r.status_code == 302

    def submitted(iid):
        return portal_db.query_one(
            "SELECT submitted FROM client_invoices WHERE id = %s", (iid,))["submitted"]

    assert submitted(d1) is True
    assert submitted(d2) is True
    assert submitted(d3_untouched) is False


def test_submit_batch_forbidden_for_client(app, world):
    d1 = _mk_invoice(world["client"], submitted=False)
    c = _client(app, CLIENT_EMAIL)
    r = c.post("/portal/admin/client-invoices/submit-batch", data={
        "csrf_token": _csrf(c), "invoice_ids": [str(d1)],
    }, follow_redirects=False)
    assert r.status_code == 302
    assert r.location.endswith("/portal")
    assert portal_db.query_one(
        "SELECT submitted FROM client_invoices WHERE id = %s", (d1,))["submitted"] is False


def test_submit_batch_empty_selection_flashes_error(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post("/portal/admin/client-invoices/submit-batch", data={
        "csrf_token": _csrf(admin),
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Select at least one invoice" in r.data
