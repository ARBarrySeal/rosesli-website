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
INT3_EMAIL = "pytest-sched-int3@example.test"
EMAILS = [ADMIN_EMAIL, INT1_EMAIL, INT2_EMAIL, INT3_EMAIL]

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


# ── Multi-interpreter staffing (Phase 5: job_interpreters join table) ─────────

def _staffed(job_id):
    return portal_db.query_all(
        "SELECT interpreter_id, slot FROM job_interpreters "
        "WHERE job_id = %s ORDER BY slot, id",
        (job_id,),
    )


def _offer_and_accept(app, admin, job_id, interpreter_id, email):
    admin.post(
        f"/portal/admin/assignments/{job_id}/offer",
        data={"interpreter_id": interpreter_id, "csrf_token": _csrf(admin)},
    )
    offer = _offer_id(job_id, interpreter_id)
    intp = _client(app, email)
    intp.post(f"/portal/offers/{offer['id']}/accept", data={"csrf_token": _csrf(intp)})


def test_confirm_fills_next_slot_without_overwriting(app, world):
    """num_required=2: first confirm holds slot 1 and leaves the job pending;
    second confirm fills slot 2, flips to confirmed, and never overwrites slot 1
    (the pre-Phase-5 bug)."""
    job_id = world["job"]
    portal_db.execute(
        "UPDATE jobs SET num_interpreters = 2, num_required = 2 WHERE id = %s", (job_id,),
    )
    admin = _client(app, ADMIN_EMAIL)
    _offer_and_accept(app, admin, job_id, world["int1"], INT1_EMAIL)
    _offer_and_accept(app, admin, job_id, world["int2"], INT2_EMAIL)

    # Confirm #1 → slot 1 filled, job still pending, int2's offer still open.
    admin.post(
        f"/portal/admin/assignments/{job_id}/confirm",
        data={"interpreter_id": world["int1"], "csrf_token": _csrf(admin)},
    )
    job = portal_db.query_one(
        "SELECT status, interpreter_1_id, interpreter_2_id FROM jobs WHERE id = %s", (job_id,),
    )
    assert job["status"] == "pending"
    assert job["interpreter_1_id"] == world["int1"]
    assert _offer_id(job_id, world["int2"])["status"] == "accepted"

    # Confirm #2 → slot 2 filled (slot 1 untouched), job confirmed.
    admin.post(
        f"/portal/admin/assignments/{job_id}/confirm",
        data={"interpreter_id": world["int2"], "csrf_token": _csrf(admin)},
    )
    job = portal_db.query_one(
        "SELECT status, interpreter_1_id, interpreter_2_id FROM jobs WHERE id = %s", (job_id,),
    )
    assert job["status"] == "confirmed"
    assert job["interpreter_1_id"] == world["int1"]  # never overwritten
    assert job["interpreter_2_id"] == world["int2"]
    assert [s["interpreter_id"] for s in _staffed(job_id)] == [world["int1"], world["int2"]]


def test_confirm_withdraws_siblings_only_when_filled(app, world):
    """num_required=1 (default job): confirming int1 withdraws int2's open offer
    and keeps int1 staffed."""
    job_id = world["job"]
    int3 = _mk_user(INT3_EMAIL, "employee")
    admin = _client(app, ADMIN_EMAIL)
    _offer_and_accept(app, admin, job_id, world["int1"], INT1_EMAIL)
    admin.post(
        f"/portal/admin/assignments/{job_id}/offer",
        data={"interpreter_id": int3, "csrf_token": _csrf(admin)},
    )

    admin.post(
        f"/portal/admin/assignments/{job_id}/confirm",
        data={"interpreter_id": world["int1"], "csrf_token": _csrf(admin)},
    )
    job = portal_db.query_one("SELECT status FROM jobs WHERE id = %s", (job_id,))
    assert job["status"] == "confirmed"
    assert _offer_id(job_id, world["int1"])["status"] == "accepted"
    assert _offer_id(job_id, int3)["status"] == "withdrawn"
    assert [s["interpreter_id"] for s in _staffed(job_id)] == [world["int1"]]


def test_edit_form_staffs_three_interpreters_and_mirrors_two(app, world):
    """set_job_interpreters: 3 ids land in the join table; legacy columns mirror
    slots 1-2 only (write-through compatibility)."""
    from portal_jobs import set_job_interpreters
    job_id = world["job"]
    int3 = _mk_user(INT3_EMAIL, "employee")
    ids = [world["int1"], world["int2"], int3]
    set_job_interpreters(COMPANY, job_id, ids)

    assert [s["interpreter_id"] for s in _staffed(job_id)] == ids
    job = portal_db.query_one(
        "SELECT interpreter_1_id, interpreter_2_id FROM jobs WHERE id = %s", (job_id,),
    )
    assert job["interpreter_1_id"] == world["int1"]
    assert job["interpreter_2_id"] == world["int2"]

    # Re-staffing with a shorter list replaces, never appends.
    set_job_interpreters(COMPANY, job_id, [int3])
    assert [s["interpreter_id"] for s in _staffed(job_id)] == [int3]
    job = portal_db.query_one(
        "SELECT interpreter_1_id, interpreter_2_id FROM jobs WHERE id = %s", (job_id,),
    )
    assert job["interpreter_1_id"] == int3
    assert job["interpreter_2_id"] is None
