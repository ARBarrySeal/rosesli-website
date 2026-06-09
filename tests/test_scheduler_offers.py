"""Pytest suite for the Usked-style scheduler: availability + job offers.

Runs against the live local Postgres bot-db (same convention as
test_portal_security.py). COMPANY is bound to COMPANY_ID from conftest.

Coverage:
  * Availability block-off → is_unavailable() reflects it
  * Coordinator offers a job → interpreter sees it pending
  * Accept → coordinator confirms → job is staffed (status confirmed)
  * Decline → drops out of the interpreter's pending list
  * Offering to an unavailable interpreter is skipped (no offer row)
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
from portal_auth import hash_password
from portal_availability import is_unavailable


COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestSched12345!"

ADMIN_EMAIL = "pytest-sched-admin@example.test"
INT1_EMAIL = "pytest-sched-int1@example.test"
INT2_EMAIL = "pytest-sched-int2@example.test"
EMAILS = [ADMIN_EMAIL, INT1_EMAIL, INT2_EMAIL]

JOB_MARKER = "pytest-sched-job-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=14)


# ── Setup / teardown ──────────────────────────────────────────────────────────

def _cleanup():
    # Jobs (and their offers, via ON DELETE CASCADE) first, then users (which
    # cascade their offers + unavailability blocks), then audit rows.
    portal_db.execute("DELETE FROM jobs WHERE notes = %s AND company = %s", (JOB_MARKER, COMPANY))
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s",
        (EMAILS, COMPANY),
    )
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
    """Three users (admin + two interpreters) and one pending job."""
    _cleanup()
    admin_id = _mk_user(ADMIN_EMAIL, "admin")
    int1_id = _mk_user(INT1_EMAIL, "employee")
    int2_id = _mk_user(INT2_EMAIL, "employee")
    job = portal_db.execute(
        "INSERT INTO jobs (company, status, event_date, num_interpreters, notes) "
        "VALUES (%s, 'pending', %s, 1, %s) RETURNING id",
        (COMPANY, EVENT_DATE, JOB_MARKER),
    )
    yield {
        "admin": admin_id, "int1": int1_id, "int2": int2_id, "job": job["id"],
    }
    _cleanup()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _login(client, email):
    with client.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        csrf = sess["csrf_token"]
    r = client.post("/login", data={"email": email, "password": PW, "csrf_token": csrf})
    assert r.status_code == 200, r.data
    # rosesli has MFA disabled → full session straight to the portal.
    assert r.get_json()["redirect"] == "/portal"
    return client


def _csrf(client):
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def _client(app, email):
    return _login(app.test_client(), email)


def _offer_id(job_id, interpreter_id):
    row = portal_db.query_one(
        "SELECT id, status FROM job_offers WHERE job_id = %s AND interpreter_id = %s AND company = %s",
        (job_id, interpreter_id, COMPANY),
    )
    return row


# ── Availability ──────────────────────────────────────────────────────────────

def test_block_off_marks_interpreter_unavailable(app, world):
    c = _client(app, INT1_EMAIL)
    r = c.post(
        "/portal/availability/add",
        data={"start_date": EVENT_DATE.isoformat(), "end_date": "", "csrf_token": _csrf(c)},
    )
    assert r.status_code in (302, 303)
    assert is_unavailable(world["int1"], COMPANY, EVENT_DATE) is True
    # A day outside the block stays available.
    assert is_unavailable(world["int1"], COMPANY, EVENT_DATE + dt.timedelta(days=3)) is False


# ── Offer → accept → confirm ──────────────────────────────────────────────────

def test_offer_shows_as_pending_for_interpreter(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post(
        f"/portal/admin/assignments/{world['job']}/offer",
        data={"interpreter_id": world["int1"], "csrf_token": _csrf(admin)},
    )
    assert r.status_code in (302, 303)
    offer = _offer_id(world["job"], world["int1"])
    assert offer and offer["status"] == "offered"

    intp = _client(app, INT1_EMAIL)
    r = intp.get("/portal/offers")
    assert r.status_code == 200
    assert str(world["job"]).encode() in r.data or b"Pending offers" in r.data


def test_accept_then_confirm_staffs_the_job(app, world):
    admin = _client(app, ADMIN_EMAIL)
    admin.post(
        f"/portal/admin/assignments/{world['job']}/offer",
        data={"interpreter_id": world["int1"], "csrf_token": _csrf(admin)},
    )
    offer = _offer_id(world["job"], world["int1"])

    intp = _client(app, INT1_EMAIL)
    r = intp.post(f"/portal/offers/{offer['id']}/accept", data={"csrf_token": _csrf(intp)})
    assert r.status_code in (302, 303)
    assert _offer_id(world["job"], world["int1"])["status"] == "accepted"

    r = admin.post(
        f"/portal/admin/assignments/{world['job']}/confirm",
        data={"interpreter_id": world["int1"], "csrf_token": _csrf(admin)},
    )
    assert r.status_code in (302, 303)
    job = portal_db.query_one(
        "SELECT status, interpreter_1_id FROM jobs WHERE id = %s", (world["job"],),
    )
    assert job["status"] == "confirmed"
    assert job["interpreter_1_id"] == world["int1"]


def test_decline_drops_out_of_pending(app, world):
    admin = _client(app, ADMIN_EMAIL)
    admin.post(
        f"/portal/admin/assignments/{world['job']}/offer",
        data={"interpreter_id": world["int2"], "csrf_token": _csrf(admin)},
    )
    offer = _offer_id(world["job"], world["int2"])

    intp = _client(app, INT2_EMAIL)
    r = intp.post(f"/portal/offers/{offer['id']}/decline", data={"csrf_token": _csrf(intp)})
    assert r.status_code in (302, 303)
    assert _offer_id(world["job"], world["int2"])["status"] == "declined"


def test_offer_skips_unavailable_interpreter(app, world):
    # int2 blocks the job's event date.
    portal_db.execute(
        "INSERT INTO interpreter_unavailability (company, user_id, start_date, end_date) "
        "VALUES (%s, %s, %s, %s)",
        (COMPANY, world["int2"], EVENT_DATE, EVENT_DATE),
    )
    admin = _client(app, ADMIN_EMAIL)
    r = admin.post(
        f"/portal/admin/assignments/{world['job']}/offer",
        data={"interpreter_id": world["int2"], "csrf_token": _csrf(admin)},
    )
    assert r.status_code in (302, 303)
    # No offer row should have been created for the unavailable interpreter.
    assert _offer_id(world["job"], world["int2"]) is None
