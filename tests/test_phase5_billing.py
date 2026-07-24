"""Phase 5 (2026-07-22 batch) — Billing section (Create Client Invoice).

Resolves the 2026-06-20 batch's open #15: the main line's differential now
wires directly into the applied rate (base_rate + differential), not just
an additive extra line. Adds a flat-rate mode.

Covers:
  * /portal/admin/client-invoices/create stays admin-only (5.1)
  * hourly: total = duration x (base_rate + differential) + incidentals + extras (5.2, 5.5)
  * hourly with no differential: unchanged duration x base_rate behavior
  * flat: total = flat_amount + incidentals, no rate x duration math at all (5.3, 5.4)
  * blank base rate still resolves from the client's account rate
  * portal_rates.set_rate() recalculates hourly invoices' base_rate/differential/
    rate_per_hour/total, and never touches flat-rate invoices
"""
import os
import secrets
from datetime import date, timedelta

import pytest

import portal_db
from portal_auth import hash_password

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase5Billing_12345!"

ADMIN_EMAIL = "pytest-p5b-admin@example.test"
EMP_EMAIL = "pytest-p5b-emp@example.test"
CLIENT_EMAIL = "pytest-p5b-client@example.test"
EMAILS = [ADMIN_EMAIL, EMP_EMAIL, CLIENT_EMAIL]

MARKER = "pytest-p5b-marker"
EVENT_DATE = date.today() + timedelta(days=40)


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
    emp_id = _mk_user(EMP_EMAIL, "employee")
    client_id = _mk_user(CLIENT_EMAIL, "client", full_name="Acme Corp", interpreter_rate=100)
    yield {"admin": admin_id, "emp": emp_id, "client": client_id}
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


def _latest_invoice():
    return portal_db.query_one(
        "SELECT * FROM client_invoices WHERE notes = %s ORDER BY id DESC LIMIT 1", (MARKER,))


# ── 1. Admin-only access ────────────────────────────────────────────────────

def test_create_form_forbidden_for_employee(app, world):
    c = _client(app, EMP_EMAIL)
    r = c.get("/portal/admin/client-invoices/create", follow_redirects=False)
    assert r.status_code == 302
    assert r.location.endswith("/portal")


def test_create_post_forbidden_for_employee(app, world):
    c = _client(app, EMP_EMAIL)
    r = c.post("/portal/admin/client-invoices/create", data={
        "csrf_token": _csrf(c), "rate_type": "hourly", "base_rate": "100",
        "duration_hours": "2", "notes": MARKER,
    }, follow_redirects=False)
    assert r.status_code == 302
    assert r.location.endswith("/portal")


# ── 2. Hourly: differential wires into the main line total ────────────────

def test_hourly_with_differential_wires_into_total(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post("/portal/admin/client-invoices/create", data={
        "csrf_token": _csrf(admin), "client_id": str(world["client"]),
        "rate_type": "hourly", "base_rate": "100", "differential": "10",
        "duration_hours": "3", "incidentals": "0", "notes": MARKER,
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    inv = _latest_invoice()
    assert float(inv["base_rate"]) == 100.0
    assert inv["differential"] == "10"
    assert float(inv["rate_per_hour"]) == 110.0  # applied rate = base + diff
    assert float(inv["total"]) == 330.0          # 3h x 110


def test_hourly_no_differential_unchanged_behavior(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post("/portal/admin/client-invoices/create", data={
        "csrf_token": _csrf(admin), "client_id": str(world["client"]),
        "rate_type": "hourly", "base_rate": "80", "duration_hours": "2",
        "incidentals": "15", "notes": MARKER,
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    inv = _latest_invoice()
    assert float(inv["rate_per_hour"]) == 80.0
    assert float(inv["total"]) == 175.0  # 2h x 80 + 15 incidentals


def test_hourly_blank_base_rate_resolves_from_client_account(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post("/portal/admin/client-invoices/create", data={
        "csrf_token": _csrf(admin), "client_id": str(world["client"]),
        "rate_type": "hourly", "duration_hours": "2", "notes": MARKER,
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    inv = _latest_invoice()
    assert float(inv["base_rate"]) == 100.0  # from portal_users.interpreter_rate


# ── 3. Flat rate: no rate x duration math ──────────────────────────────────

def test_flat_rate_ignores_rate_and_duration_math(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post("/portal/admin/client-invoices/create", data={
        "csrf_token": _csrf(admin), "client_id": str(world["client"]),
        "rate_type": "flat", "flat_amount": "500", "duration_hours": "99",
        "base_rate": "9999", "incidentals": "25", "notes": MARKER,
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    inv = _latest_invoice()
    assert inv["rate_type"] == "flat"
    assert inv["rate_per_hour"] is None
    assert inv["base_rate"] is None
    assert inv["differential"] is None
    assert float(inv["total"]) == 525.0  # 500 flat + 25 incidentals, NOT duration/rate driven


def test_flat_rate_requires_amount(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post("/portal/admin/client-invoices/create", data={
        "csrf_token": _csrf(admin), "client_id": str(world["client"]),
        "rate_type": "flat", "notes": MARKER,
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Flat amount is required" in r.data


# ── 4. portal_rates.set_rate() recalc respects rate_type ───────────────────

def _mk_invoice(client_id, dos, status="unpaid", rate_type="hourly", base_rate=100,
                 differential=None, duration_hours=2, rate_per_hour=100, total=200):
    row = portal_db.execute(
        "INSERT INTO client_invoices (company, client_id, notes, date_of_service, status, "
        "rate_type, base_rate, differential, duration_hours, rate_per_hour, total) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (COMPANY, client_id, MARKER, dos, status, rate_type, base_rate,
         differential, duration_hours, rate_per_hour, total),
    )
    return row["id"]


def test_set_rate_recalcs_hourly_with_differential(world):
    import portal_rates
    inv_id = _mk_invoice(world["client"], EVENT_DATE, differential="10")
    portal_rates.set_rate(COMPANY, world["client"], 150, EVENT_DATE - timedelta(days=1),
                          created_by=world["admin"])
    inv = portal_db.query_one("SELECT * FROM client_invoices WHERE id = %s", (inv_id,))
    assert float(inv["base_rate"]) == 150.0
    assert float(inv["rate_per_hour"]) == 160.0  # 150 base + 10 diff
    assert float(inv["total"]) == 320.0          # 2h x 160


def test_set_rate_never_touches_flat_invoices(world):
    import portal_rates
    inv_id = _mk_invoice(world["client"], EVENT_DATE, rate_type="flat",
                         base_rate=None, rate_per_hour=None, total=500)
    portal_rates.set_rate(COMPANY, world["client"], 150, EVENT_DATE - timedelta(days=1),
                          created_by=world["admin"])
    inv = portal_db.query_one("SELECT * FROM client_invoices WHERE id = %s", (inv_id,))
    assert inv["rate_per_hour"] is None
    assert float(inv["total"]) == 500.0


# ── 5. Rendered form has the new controls ───────────────────────────────────

def test_create_form_renders_rate_type_and_differential_controls(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal/admin/client-invoices/create").data.decode()
    assert 'name="rate_type"' in html
    assert 'name="base_rate"' in html
    assert 'name="differential"' in html
    assert 'name="flat_amount"' in html
    assert 'ci-total-amount' in html
