"""Phase 4 — assignments polish + scheduler email routing (Rose SLI).

Covers the genuinely-new Phase 4 work (the offer/confirm machinery itself
shipped with the Usked scheduler and is exercised by test_scheduler_offers.py):

  * Duration auto-calc from start/end times (portal_jobs._compute_duration)
  * Offer-skip flash names the interpreter, not "#<id>"
  * Client base rate (stored in interpreter_rate) is pulled onto the assignment
    when the admin leaves Client rate ($) blank
  * /api/clients returns each client's base rate for the form's auto-fill
  * Scheduler emails route to PORTAL_TEST_EMAIL when set (offer/confirm/withdraw)
  * Interpreter specialty drives offers-by-category sorting
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
import portal_email
from portal_auth import hash_password


COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase4_12345!"

ADMIN_EMAIL = "pytest-p4-admin@example.test"
INT1_EMAIL = "pytest-p4-int1@example.test"
INT2_EMAIL = "pytest-p4-int2@example.test"
CLIENT_EMAIL = "pytest-p4-client@example.test"
EMAILS = [ADMIN_EMAIL, INT1_EMAIL, INT2_EMAIL, CLIENT_EMAIL]

JOB_MARKER = "pytest-p4-job-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=21)


# ── Setup / teardown ──────────────────────────────────────────────────────────

def _cleanup():
    portal_db.execute("DELETE FROM jobs WHERE notes = %s AND company = %s", (JOB_MARKER, COMPANY))
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY),
    )
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))


def _mk_user(email, role, **cols):
    base = "INSERT INTO portal_users (email, password_hash, full_name, role, company, active"
    vals = [email, hash_password(PW), cols.pop("full_name", f"Pytest {role.title()}"), role, COMPANY, True]
    extra_cols = ""
    for k, v in cols.items():
        extra_cols += f", {k}"
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    row = portal_db.execute(
        f"{base}{extra_cols}) VALUES ({placeholders}) RETURNING id", tuple(vals),
    )
    return row["id"]


@pytest.fixture
def world():
    _cleanup()
    admin_id = _mk_user(ADMIN_EMAIL, "admin")
    int1_id = _mk_user(INT1_EMAIL, "employee", full_name="Alice Legal", specialty="Legal")
    int2_id = _mk_user(INT2_EMAIL, "employee", full_name="Bob Medical", specialty="Medical")
    client_id = _mk_user(CLIENT_EMAIL, "client", full_name="Acme Corp", interpreter_rate=125)
    job = portal_db.execute(
        "INSERT INTO jobs (company, status, event_date, num_interpreters, notes) "
        "VALUES (%s, 'pending', %s, 1, %s) RETURNING id",
        (COMPANY, EVENT_DATE, JOB_MARKER),
    )
    yield {
        "admin": admin_id, "int1": int1_id, "int2": int2_id,
        "client": client_id, "job": job["id"],
    }
    _cleanup()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _login(client, email):
    with client.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        csrf = sess["csrf_token"]
    r = client.post("/login", data={"email": email, "password": PW, "csrf_token": csrf})
    assert r.status_code == 200, r.data
    assert r.get_json()["redirect"] == "/portal"
    return client


def _csrf(client):
    with client.session_transaction() as sess:
        return sess["csrf_token"]


def _client(app, email):
    return _login(app.test_client(), email)


def _flashes(client):
    with client.session_transaction() as sess:
        return [m for _cat, m in sess.get("_flashes", [])]


# ── 1. Duration auto-calc ───────────────────────────────────────────────────

@pytest.mark.parametrize("start,end,expected", [
    ("9:00 AM", "11:30 AM", "2.5"),
    ("9:00 AM", "11:00 AM", "2"),
    ("2:00 PM", "5:00 PM", "3"),
    ("14:00", "16:30", "2.5"),
    ("9:15 AM", "10:00 AM", "0.75"),
])
def test_compute_duration_parses_common_formats(start, end, expected):
    from portal_jobs import _compute_duration
    assert _compute_duration(start, end) == expected


@pytest.mark.parametrize("start,end", [
    ("", "11:00 AM"), ("9:00 AM", ""), ("nonsense", "later"),
    ("5:00 PM", "9:00 AM"),  # end before start → don't guess overnight
])
def test_compute_duration_returns_none_when_unparseable(start, end):
    from portal_jobs import _compute_duration
    assert _compute_duration(start, end) is None


def test_create_assignment_autofills_blank_duration(app, world):
    admin = _client(app, ADMIN_EMAIL)
    admin.post("/portal/admin/assignments/new", data={
        "csrf_token": _csrf(admin),
        "status": "pending",
        "event_date": EVENT_DATE.isoformat(),
        "start_time": "9:00 AM", "end_time": "11:30 AM",
        "duration": "",  # blank → should be auto-filled
        "notes": JOB_MARKER,
    })
    job = portal_db.query_one(
        "SELECT duration FROM jobs WHERE notes = %s AND company = %s ORDER BY id DESC",
        (JOB_MARKER, COMPANY),
    )
    assert job["duration"] == "2.5"


def test_create_assignment_respects_explicit_duration(app, world):
    admin = _client(app, ADMIN_EMAIL)
    admin.post("/portal/admin/assignments/new", data={
        "csrf_token": _csrf(admin),
        "status": "pending",
        "start_time": "9:00 AM", "end_time": "11:30 AM",
        "duration": "half day",
        "notes": JOB_MARKER,
    })
    job = portal_db.query_one(
        "SELECT duration FROM jobs WHERE notes = %s AND company = %s ORDER BY id DESC",
        (JOB_MARKER, COMPANY),
    )
    assert job["duration"] == "half day"


# ── 2. Offer-skip flash names the interpreter ─────────────────────────────────

def test_offer_skip_flash_uses_name_not_id(app, world):
    # int1 blocks the event date → offering is skipped with a reason.
    portal_db.execute(
        "INSERT INTO interpreter_unavailability (company, user_id, start_date, end_date) "
        "VALUES (%s, %s, %s, %s)",
        (COMPANY, world["int1"], EVENT_DATE, EVENT_DATE),
    )
    admin = _client(app, ADMIN_EMAIL)
    admin.post(
        f"/portal/admin/assignments/{world['job']}/offer",
        data={"interpreter_id": world["int1"], "csrf_token": _csrf(admin)},
    )
    msgs = " ".join(_flashes(admin))
    assert "Alice Legal" in msgs
    assert f"#{world['int1']}" not in msgs


# ── 3. Pull client base rate ──────────────────────────────────────────────────

def test_create_assignment_pulls_client_rate_when_blank(app, world):
    admin = _client(app, ADMIN_EMAIL)
    admin.post("/portal/admin/assignments/new", data={
        "csrf_token": _csrf(admin),
        "status": "pending",
        "client_id": world["client"],
        "client_rate": "",  # blank → pull from client's base rate (125)
        "notes": JOB_MARKER,
    })
    job = portal_db.query_one(
        "SELECT client_rate FROM jobs WHERE notes = %s AND company = %s ORDER BY id DESC",
        (JOB_MARKER, COMPANY),
    )
    assert float(job["client_rate"]) == 125.0


def test_create_assignment_respects_explicit_client_rate(app, world):
    admin = _client(app, ADMIN_EMAIL)
    admin.post("/portal/admin/assignments/new", data={
        "csrf_token": _csrf(admin),
        "status": "pending",
        "client_id": world["client"],
        "client_rate": "200",
        "notes": JOB_MARKER,
    })
    job = portal_db.query_one(
        "SELECT client_rate FROM jobs WHERE notes = %s AND company = %s ORDER BY id DESC",
        (JOB_MARKER, COMPANY),
    )
    assert float(job["client_rate"]) == 200.0


def test_api_clients_returns_rate(app, world):
    admin = _client(app, ADMIN_EMAIL)
    r = admin.get("/api/clients")
    assert r.status_code == 200
    rows = {c["id"]: c for c in r.get_json()}
    assert world["client"] in rows
    assert rows[world["client"]]["rate"] == 125.0


# ── 4. Scheduler email routing via PORTAL_TEST_EMAIL ──────────────────────────

class _CaptureSMTP:
    sent_to = None
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, **kw): pass
    def login(self, u, p): pass
    def send_message(self, msg): _CaptureSMTP.sent_to = msg["To"]


def _smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "user@example.test")
    monkeypatch.setenv("SMTP_PASS", "pw")
    monkeypatch.setattr(portal_email.smtplib, "SMTP", _CaptureSMTP)


def test_offer_email_routes_to_test_address_when_set(monkeypatch):
    _smtp_env(monkeypatch)
    monkeypatch.setenv("PORTAL_TEST_EMAIL", "rosecharlesrose@gmail.com")
    _CaptureSMTP.sent_to = None
    portal_email.send_offer_email(
        "real-interpreter@example.test", "Alice", {"setting": "Legal"},
        "http://x/portal/offers", "Rose SLI",
    )
    assert _CaptureSMTP.sent_to == "rosecharlesrose@gmail.com"


def test_confirm_email_uses_real_address_when_unset(monkeypatch):
    _smtp_env(monkeypatch)
    monkeypatch.delenv("PORTAL_TEST_EMAIL", raising=False)
    _CaptureSMTP.sent_to = None
    portal_email.send_confirm_email(
        "real-interpreter@example.test", "Alice", {"setting": "Legal"},
        "http://x/portal/offers", "Rose SLI",
    )
    assert _CaptureSMTP.sent_to == "real-interpreter@example.test"


def test_withdraw_email_exists_and_routes(monkeypatch):
    _smtp_env(monkeypatch)
    monkeypatch.setenv("PORTAL_TEST_EMAIL", "rosecharlesrose@gmail.com")
    _CaptureSMTP.sent_to = None
    portal_email.send_withdraw_email(
        "real-interpreter@example.test", "Alice", {"setting": "Legal"},
        "http://x/portal/offers", "Rose SLI",
    )
    assert _CaptureSMTP.sent_to == "rosecharlesrose@gmail.com"


# ── 5. Offers-by-category (interpreter specialty) ─────────────────────────────

def test_interpreters_for_category_sorts_matches_first(world):
    from portal_jobs import interpreters_for_category
    rows = interpreters_for_category(COMPANY, "Medical")
    names = [r["full_name"] for r in rows]
    # Bob Medical (specialty match) should come before non-matching interpreters.
    assert names.index("Bob Medical") < names.index("Alice Legal")
    assert rows[names.index("Bob Medical")]["specialty_match"] is True
    assert rows[names.index("Alice Legal")]["specialty_match"] is False
