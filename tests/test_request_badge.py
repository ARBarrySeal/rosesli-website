"""Incoming-request badge — the safety net for a silent email failure.

/api/request always persists the job row even when the notification email
fails (main.py: "a total email failure does not block persisting the lead"),
so before this the only signal a lead had arrived was that email. These tests
lock the in-portal signal:

  * pending_request_count counts only *pending* *public_request* jobs, scoped
    to the company
  * the admin nav badge and the dashboard "New Requests" card show that count
  * both go quiet once the request is actioned (no permanent red dot)
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
from portal_auth import hash_password

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestBadge_12345!"

ADMIN_EMAIL = "pytest-badge-admin@example.test"
INT_EMAIL = "pytest-badge-int@example.test"
EMAILS = [ADMIN_EMAIL, INT_EMAIL]

JOB_MARKER = "pytest-badge-job-marker"


def _cleanup():
    portal_db.execute(
        "DELETE FROM jobs WHERE notes = %s AND company = %s", (JOB_MARKER, COMPANY),
    )
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY),
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
    _cleanup()
    admin_id = _mk_user(ADMIN_EMAIL, "admin")
    int_id = _mk_user(INT_EMAIL, "employee")
    yield {"admin": admin_id, "int": int_id}
    _cleanup()


def _mk_request(status="pending", source="public_request", **cols):
    cols.setdefault("company", COMPANY)
    cols.setdefault("notes", JOB_MARKER)
    cols["status"] = status
    cols["source"] = source
    keys = list(cols.keys())
    placeholders = ", ".join(["%s"] * len(keys))
    row = portal_db.execute(
        f"INSERT INTO jobs ({', '.join(keys)}) VALUES ({placeholders}) RETURNING id",
        tuple(cols[k] for k in keys),
    )
    return row["id"]


def _client(app, email):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        csrf = sess["csrf_token"]
    r = c.post("/login", data={"email": email, "password": PW, "csrf_token": csrf})
    assert r.status_code == 200, r.data
    return c


# ── count helper ──────────────────────────────────────────────────────────────

def test_count_is_zero_with_no_requests(world):
    from portal_jobs import pending_request_count
    assert pending_request_count(COMPANY) == 0


def test_count_includes_pending_public_requests(world):
    from portal_jobs import pending_request_count
    _mk_request()
    _mk_request()
    assert pending_request_count(COMPANY) == 2


def test_count_ignores_actioned_requests(world):
    """Confirming a request must clear it — otherwise the badge never goes dark
    and admins learn to ignore it."""
    from portal_jobs import pending_request_count
    _mk_request(status="confirmed")
    _mk_request(status="cancelled")
    assert pending_request_count(COMPANY) == 0


def test_count_ignores_manually_created_assignments(world):
    """Only jobs that came in through the public form are 'requests' — an admin
    booking one by hand already knows about it."""
    from portal_jobs import pending_request_count
    _mk_request(source="manual")
    assert pending_request_count(COMPANY) == 0


def test_count_is_company_scoped(world):
    from portal_jobs import pending_request_count
    other = "dod" if COMPANY == "rosesli" else "rosesli"
    _mk_request(company=other)
    assert pending_request_count(COMPANY) == 0


# ── nav badge ─────────────────────────────────────────────────────────────────

def test_admin_nav_shows_badge_when_requests_waiting(app, world):
    _mk_request()
    html = _client(app, ADMIN_EMAIL).get("/portal").data.decode()
    nav = html.split("Incoming Requests", 1)[1][:160]
    assert "badge-pending" in nav


def test_admin_nav_has_no_badge_when_inbox_empty(app, world):
    html = _client(app, ADMIN_EMAIL).get("/portal").data.decode()
    assert "Incoming Requests" in html
    nav = html.split("Incoming Requests", 1)[1][:160]
    assert "badge-pending" not in nav


def test_interpreter_view_unaffected(app, world):
    """Interpreters have no requests link; the count must not leak into their nav."""
    _mk_request()
    html = _client(app, INT_EMAIL).get("/portal").data.decode()
    assert "Incoming Requests" not in html


# ── dashboard card ────────────────────────────────────────────────────────────

def test_dashboard_card_shows_pending_count(app, world):
    _mk_request()
    _mk_request()
    html = _client(app, ADMIN_EMAIL).get("/portal").data.decode()
    assert "New Requests" in html
    card = html.split("New Requests", 1)[1][:200]
    assert ">2<" in card


def test_dashboard_card_links_to_the_inbox(app, world):
    html = _client(app, ADMIN_EMAIL).get("/portal").data.decode()
    assert '<a href="/portal/requests"' in html
