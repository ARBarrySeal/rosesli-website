"""Phase 4 (2026-07-22 batch) — Administrative Assignments form rework.

NOTE: `tests/test_phase4.py` already exists and covers a *different*,
earlier-shipped "Phase 4" (assignments polish + scheduler email routing,
2026-06-xx batch) — numbering collides across batches, so this file is
named for the batch instead to avoid clobbering it. See
docs/portal-feature-batch-2026-07-22.md Phase 4 for the source spec.

Covers the genuinely-new Phase 4 work:

  * _interpreters_for_form: filters to available-on-date, always keeps
    already-staffed interpreters even if a later block would hide them
  * /api/interpreters?date= filters live for the form's date-change refetch
  * _parse_job_form no longer collects setting/client_address/deaf_clients —
    editing an assignment leaves those legacy columns untouched
  * _clients() exposes phone/point_of_contact for the POC auto-fill
  * dashboard admin shortcut to the create-assignment form
  * Format/Dress code persist as the new fixed dropdown values
  * removed fields (client_name, client_address, setting, deaf_clients,
    interpreter search box) are gone from the rendered form
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
from portal_auth import hash_password
from portal_jobs import _interpreters_for_form, _clients

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase4Rework_12345!"

ADMIN_EMAIL = "pytest-p4rw-admin@example.test"
INT1_EMAIL = "pytest-p4rw-int1@example.test"
INT2_EMAIL = "pytest-p4rw-int2@example.test"
CLIENT_EMAIL = "pytest-p4rw-client@example.test"
EMAILS = [ADMIN_EMAIL, INT1_EMAIL, INT2_EMAIL, CLIENT_EMAIL]

JOB_MARKER = "pytest-p4rw-job-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=30)


def _cleanup():
    portal_db.execute(
        "DELETE FROM jobs WHERE (notes = %s OR interpreter_notes = %s OR admin_notes = %s) AND company = %s",
        (JOB_MARKER, JOB_MARKER, JOB_MARKER, COMPANY),
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
    int1_id = _mk_user(INT1_EMAIL, "employee", full_name="Casey Available")
    int2_id = _mk_user(INT2_EMAIL, "employee", full_name="Drew Blocked")
    client_id = _mk_user(CLIENT_EMAIL, "client", full_name="Acme Corp",
                          phone="619-555-0100", point_of_contact="Jamie POC")
    yield {"admin": admin_id, "int1": int1_id, "int2": int2_id, "client": client_id}
    _cleanup()


def _block(user_id, start, end=None):
    portal_db.execute(
        "INSERT INTO interpreter_unavailability (company, user_id, start_date, end_date) "
        "VALUES (%s, %s, %s, %s)",
        (COMPANY, user_id, start, end or start),
    )


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


def _mk_job(**cols):
    cols.setdefault("company", COMPANY)
    cols.setdefault("status", "pending")
    cols.setdefault("interpreter_notes", JOB_MARKER)
    keys = list(cols.keys())
    placeholders = ", ".join(["%s"] * len(keys))
    row = portal_db.execute(
        f"INSERT INTO jobs ({', '.join(keys)}) VALUES ({placeholders}) RETURNING id",
        tuple(cols[k] for k in keys),
    )
    return row["id"]


# ── 1. _interpreters_for_form ───────────────────────────────────────────────

def test_excludes_blocked_interpreter_on_date(world):
    _block(world["int2"], EVENT_DATE.isoformat())
    rows = _interpreters_for_form(COMPANY, EVENT_DATE)
    ids = {r["id"] for r in rows}
    assert world["int1"] in ids
    assert world["int2"] not in ids


def test_no_date_returns_everyone(world):
    _block(world["int2"], EVENT_DATE.isoformat())
    rows = _interpreters_for_form(COMPANY, None)
    ids = {r["id"] for r in rows}
    assert world["int1"] in ids
    assert world["int2"] in ids


def test_staffed_interpreter_kept_even_if_blocked(world):
    _block(world["int2"], EVENT_DATE.isoformat())
    rows = _interpreters_for_form(COMPANY, EVENT_DATE, staffed_ids=[world["int2"]])
    ids = {r["id"] for r in rows}
    assert world["int2"] in ids


# ── 2. /api/interpreters?date= ──────────────────────────────────────────────

def test_interpreters_api_filters_by_date(app, world):
    _block(world["int2"], EVENT_DATE.isoformat())
    admin = _client(app, ADMIN_EMAIL)
    r = admin.get(f"/api/interpreters?date={EVENT_DATE.isoformat()}")
    assert r.status_code == 200
    names = {row["name"] for row in r.get_json()}
    assert "Available, Casey" in names or any("Casey" in n for n in names)
    assert not any("Drew" in n for n in names)


def test_interpreters_api_forbidden_for_non_admin(app, world):
    c = _client(app, INT1_EMAIL)
    r = c.get("/api/interpreters")
    assert r.status_code == 302


# ── 3. _clients() exposes POC auto-fill data ────────────────────────────────

def test_clients_includes_phone_and_poc(world):
    rows = _clients(COMPANY)
    row = next(r for r in rows if r["id"] == world["client"])
    assert row["phone"] == "619-555-0100"
    assert row["point_of_contact"] == "Jamie POC"


# ── 4. Editing no longer wipes legacy setting/client_address ───────────────

def test_edit_preserves_legacy_setting_and_client_address(app, world):
    job_id = _mk_job(
        status="pending", setting="Legacy Medical", client_address="123 Legacy St",
    )
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post(f"/portal/admin/assignments/{job_id}/edit", data={
        "csrf_token": _csrf(admin), "status": "pending",
        "event_date": EVENT_DATE.isoformat(),
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    row = portal_db.query_one("SELECT setting, client_address FROM jobs WHERE id = %s", (job_id,))
    assert row["setting"] == "Legacy Medical"
    assert row["client_address"] == "123 Legacy St"


# ── 5. Format / Dress code persist as the new dropdown values ──────────────

def test_create_assignment_persists_format_and_dress_code(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post("/portal/admin/assignments/new", data={
        "csrf_token": _csrf(admin), "status": "pending",
        "service_format": "VRI", "dress_code": "Business Casual",
        "interpreter_notes": JOB_MARKER,
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    row = portal_db.query_one(
        "SELECT service_format, dress_code FROM jobs WHERE interpreter_notes = %s "
        "ORDER BY id DESC LIMIT 1", (JOB_MARKER,))
    assert row["service_format"] == "VRI"
    assert row["dress_code"] == "Business Casual"


# ── 6. Rendered form: removed fields gone, new bits present ────────────────

def test_new_assignment_form_drops_removed_fields(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal/admin/assignments/new").data.decode()
    assert 'name="client_name"' not in html
    assert 'name="client_address"' not in html
    assert 'name="setting"' not in html
    assert 'name="deaf_clients"' not in html
    assert 'class="interp-search' not in html
    assert 'data-poc-name' in html
    assert 'name="service_format"' in html
    assert 'name="dress_code"' in html


def test_edit_form_renders_with_staffed_interpreter_and_consumers(app, world):
    job_id = _mk_job(status="confirmed", event_date=EVENT_DATE, client_id=world["client"])
    portal_db.execute(
        "INSERT INTO job_interpreters (job_id, interpreter_id, interpreter_name, slot) "
        "VALUES (%s, %s, %s, 1)",
        (job_id, world["int1"], "Casey Available"),
    )
    portal_db.execute(
        "INSERT INTO job_consumers (job_id, name, email) VALUES (%s, %s, %s)",
        (job_id, "Consumer One", "consumer1@example.test"),
    )
    admin = _client(app, ADMIN_EMAIL)
    r = admin.get(f"/portal/admin/assignments/{job_id}/edit")
    assert r.status_code == 200
    html = r.data.decode()
    assert "Casey Available" in html or "Available, Casey" in html
    assert "Consumer One" in html
    assert "remove-consumer" in html


def test_dashboard_has_admin_assignments_shortcut(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal").data.decode()
    assert "/portal/admin/assignments/new" in html
    assert "Administrative Assignments" in html
