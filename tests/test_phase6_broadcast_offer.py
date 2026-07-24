"""Phase 6 (2026-07-22 batch) — Send Offer blast.

Covers:
  * /portal/admin/assignments/<id>/broadcast offers to every active
    interpreter at once, additive to the existing targeted offer (6.1)
  * broadcast still skips anyone already booked or blocked off that day
    (same guard as the targeted flow)
  * broadcast is admin-only
  * the broadcast email's schedule line is zip-only, never the full street
    address (6.2) — verified directly against portal_email._job_when_zip_only
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
import portal_email
from portal_auth import hash_password

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase6_12345!"

ADMIN_EMAIL = "pytest-p6-admin@example.test"
INT1_EMAIL = "pytest-p6-int1@example.test"
INT2_EMAIL = "pytest-p6-int2@example.test"
INT3_EMAIL = "pytest-p6-int3@example.test"
EMAILS = [ADMIN_EMAIL, INT1_EMAIL, INT2_EMAIL, INT3_EMAIL]

JOB_MARKER = "pytest-p6-job-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=45)


def _cleanup():
    portal_db.execute("DELETE FROM jobs WHERE notes = %s AND company = %s", (JOB_MARKER, COMPANY))
    portal_db.execute("DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY))
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))


def _mk_user(email, role):
    row = portal_db.execute(
        "INSERT INTO portal_users (email, password_hash, full_name, role, company, active) "
        "VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id",
        (email, hash_password(PW), f"Pytest {role.title()}", role, COMPANY),
    )
    return row["id"]


@pytest.fixture
def world():
    _cleanup()
    admin_id = _mk_user(ADMIN_EMAIL, "admin")
    int1_id = _mk_user(INT1_EMAIL, "employee")
    int2_id = _mk_user(INT2_EMAIL, "employee")
    int3_id = _mk_user(INT3_EMAIL, "employee")
    job = portal_db.execute(
        "INSERT INTO jobs (company, status, event_date, num_interpreters, notes, "
        "  event_address, event_zip) "
        "VALUES (%s, 'pending', %s, 1, %s, %s, %s) RETURNING id",
        (COMPANY, EVENT_DATE, JOB_MARKER, "123 Secret St, San Diego", "92101"),
    )
    yield {"admin": admin_id, "int1": int1_id, "int2": int2_id, "int3": int3_id, "job": job["id"]}
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


def _offer(job_id, interpreter_id):
    return portal_db.query_one(
        "SELECT status FROM job_offers WHERE job_id = %s AND interpreter_id = %s AND company = %s",
        (job_id, interpreter_id, COMPANY),
    )


# ── 1. Broadcast offers to everyone ─────────────────────────────────────────

def test_broadcast_offers_to_all_active_interpreters(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post(f"/portal/admin/assignments/{world['job']}/broadcast",
                   data={"csrf_token": _csrf(admin)}, follow_redirects=False)
    assert r.status_code in (302, 303), r.data
    for iid in (world["int1"], world["int2"], world["int3"]):
        offer = _offer(world["job"], iid)
        assert offer and offer["status"] == "offered"


def test_broadcast_is_additive_to_targeted_offer(app, world):
    admin = _client(app, ADMIN_EMAIL)
    admin.post(f"/portal/admin/assignments/{world['job']}/offer",
              data={"interpreter_id": world["int1"], "csrf_token": _csrf(admin)})
    admin.post(f"/portal/admin/assignments/{world['job']}/broadcast",
              data={"csrf_token": _csrf(admin)})
    # int1's targeted offer survives, and the broadcast reaches int2/int3 too.
    assert _offer(world["job"], world["int1"])["status"] == "offered"
    assert _offer(world["job"], world["int2"])["status"] == "offered"
    assert _offer(world["job"], world["int3"])["status"] == "offered"


# ── 2. Broadcast respects availability/booking guards ──────────────────────

def test_broadcast_skips_unavailable_interpreter(app, world):
    intp = _client(app, INT1_EMAIL)
    intp.post("/portal/availability/add", data={
        "start_date": EVENT_DATE.isoformat(), "end_date": "", "csrf_token": _csrf(intp),
    })
    admin = _client(app, ADMIN_EMAIL)
    admin.post(f"/portal/admin/assignments/{world['job']}/broadcast",
              data={"csrf_token": _csrf(admin)})
    assert _offer(world["job"], world["int1"]) is None
    assert _offer(world["job"], world["int2"])["status"] == "offered"


# ── 3. Admin-only ────────────────────────────────────────────────────────────

def test_broadcast_forbidden_for_employee(app, world):
    c = _client(app, INT1_EMAIL)
    r = c.post(f"/portal/admin/assignments/{world['job']}/broadcast",
              data={"csrf_token": _csrf(c)}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert _offer(world["job"], world["int2"]) is None


# ── 4. Broadcast email is zip-only, never the full address ─────────────────

def test_broadcast_schedule_line_omits_full_address(world):
    job = portal_db.query_one("SELECT * FROM jobs WHERE id = %s", (world["job"],))
    line = portal_email._job_when_zip_only(job)
    assert "92101" in line
    assert "123 Secret St" not in line
    assert "Secret" not in line


def test_targeted_schedule_line_still_includes_full_address(world):
    # The targeted flow (send_offer_email / _job_when) is untouched by Phase 6 —
    # only the broadcast email is privacy-trimmed.
    job = portal_db.query_one("SELECT * FROM jobs WHERE id = %s", (world["job"],))
    line = portal_email._job_when(job)
    assert "123 Secret St" in line


# ── 5. Broadcast button renders on the assignment detail page ──────────────

def test_assignment_detail_shows_broadcast_button(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get(f"/portal/assignments/{world['job']}").data.decode()
    assert f'/portal/admin/assignments/{world["job"]}/broadcast' in html
    assert "Broadcast to all interpreters" in html
