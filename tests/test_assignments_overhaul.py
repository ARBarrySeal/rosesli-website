"""Phase 6 (2026-07 batch) — Assignments page overhaul.

Covers the genuinely-new Phase 6 work:

  * hours_minutes Jinja filter: decimal-hours → "N hr M min", edge cases and
    unparseable-legacy pass-through
  * job_consumers CRUD: set_job_consumers replaces rows, _parse_consumers pairs
    the repeated form fields and drops blank rows
  * admin_notes is NEVER rendered on the detail page for an interpreter (the
    is_admin template gate), while interpreter_notes is visible to both
  * legacy event_address renders as the fallback when the split street/city/
    state fields are empty; the split wins when set
  * dod tenant nav/heading unchanged ("Assignments", no rosesli renames)

Runs against the live local Postgres botdb (same convention as the other
portal suites).
"""

import datetime as dt
import secrets

import pytest
from flask import g
from werkzeug.datastructures import MultiDict

import portal_db
from portal_auth import hash_password
from portal_jobs import _parse_consumers, job_consumer_rows, set_job_consumers

COMPANY = "rosesli"
PW = "pytest-Str0ng!pass"

ADMIN_EMAIL = "pytest-asn6-admin@example.test"
INT_EMAIL = "pytest-asn6-int@example.test"
EMAILS = [ADMIN_EMAIL, INT_EMAIL]

JOB_MARKER = "pytest-asn6-job-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=21)


# ── Setup / teardown ──────────────────────────────────────────────────────────

def _cleanup():
    portal_db.execute(
        "DELETE FROM jobs WHERE setting = %s AND company = %s", (JOB_MARKER, COMPANY)
    )
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = ANY(%s)", (EMAILS,)
    )
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))


def _mk_user(email, role, company=COMPANY):
    row = portal_db.execute(
        "INSERT INTO portal_users (email, password_hash, full_name, role, company, active) "
        "VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id",
        (email, hash_password(PW), f"Pytest {role.title()}", role, company),
    )
    return row["id"]


def _mk_job(**cols):
    cols = {"company": COMPANY, "status": "confirmed", "event_date": EVENT_DATE,
            "setting": JOB_MARKER, **cols}
    names = ", ".join(cols)
    ph = ", ".join(["%s"] * len(cols))
    row = portal_db.execute(
        f"INSERT INTO jobs ({names}) VALUES ({ph}) RETURNING id", tuple(cols.values())
    )
    return row["id"]


@pytest.fixture
def world():
    _cleanup()
    admin_id = _mk_user(ADMIN_EMAIL, "admin")
    int_id = _mk_user(INT_EMAIL, "employee")
    yield {"admin": admin_id, "int": int_id}
    _cleanup()


def _login(client, email):
    with client.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        csrf = sess["csrf_token"]
    r = client.post("/login", data={"email": email, "password": PW, "csrf_token": csrf})
    assert r.status_code == 200, r.data
    assert r.get_json()["redirect"] == "/portal"
    return client


def _client(app, email):
    return _login(app.test_client(), email)


# ── hours_minutes filter ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("2.5", "2 hr 30 min"),
    ("2", "2 hr"),
    ("1.25", "1 hr 15 min"),
    ("0.75", "45 min"),
    ("0", "0 min"),
    ("2 hours", "2 hours"),   # legacy free text passes through
    ("", ""),
    (None, None),
])
def test_hours_minutes_filter(app, value, expected):
    assert app.jinja_env.filters["hours_minutes"](value) == expected


# ── Consumers CRUD ────────────────────────────────────────────────────────────

def test_parse_consumers_pairs_and_drops_blanks():
    form = MultiDict([
        ("consumer_names", "Ann Alpha"), ("consumer_emails", "ann@example.test"),
        ("consumer_names", ""), ("consumer_emails", ""),          # blank row dropped
        ("consumer_names", "Bo Beta"), ("consumer_emails", ""),   # name-only kept
    ])
    assert _parse_consumers(form) == [
        ("Ann Alpha", "ann@example.test"),
        ("Bo Beta", None),
    ]


def test_set_job_consumers_replaces(world):
    job_id = _mk_job()
    set_job_consumers(job_id, [("Ann", "ann@example.test"), ("Bo", None)])
    rows = job_consumer_rows(job_id)
    assert [(r["name"], r["email"]) for r in rows] == [("Ann", "ann@example.test"), ("Bo", None)]
    # The form is authoritative: a second save replaces, never appends.
    set_job_consumers(job_id, [("Cy", "cy@example.test")])
    rows = job_consumer_rows(job_id)
    assert [(r["name"], r["email"]) for r in rows] == [("Cy", "cy@example.test")]


# ── admin_notes gate ──────────────────────────────────────────────────────────

def test_admin_notes_hidden_from_interpreter(app, world):
    job_id = _mk_job(
        interpreter_1_id=world["int"],
        admin_notes="SECRET-ADMIN-ONLY-NOTE",
        interpreter_notes="VISIBLE-TO-INTERPRETER",
    )
    html = _client(app, INT_EMAIL).get(f"/portal/assignments/{job_id}").data.decode()
    assert "VISIBLE-TO-INTERPRETER" in html
    assert "SECRET-ADMIN-ONLY-NOTE" not in html

    html = _client(app, ADMIN_EMAIL).get(f"/portal/assignments/{job_id}").data.decode()
    assert "VISIBLE-TO-INTERPRETER" in html
    assert "SECRET-ADMIN-ONLY-NOTE" in html


# ── Address fallback ──────────────────────────────────────────────────────────

def test_legacy_address_fallback(app, world):
    job_id = _mk_job(event_address="999 Legacy Way, Old Town")
    html = _client(app, ADMIN_EMAIL).get(f"/portal/assignments/{job_id}").data.decode()
    assert "999 Legacy Way, Old Town" in html


def test_split_address_wins_over_legacy(app, world):
    job_id = _mk_job(
        event_address="999 Legacy Way, Old Town",
        event_street="123 Split St", event_city="San Diego", event_state="CA",
        room_number="Suite 210",
    )
    html = _client(app, ADMIN_EMAIL).get(f"/portal/assignments/{job_id}").data.decode()
    assert "123 Split St, San Diego, CA" in html
    assert "999 Legacy Way, Old Town" not in html
    assert "Suite 210" in html


# ── dod tenant unchanged ──────────────────────────────────────────────────────

def test_dod_nav_and_heading_unchanged(app, world):
    """dod keeps plain 'Assignments' — no rosesli renames leak across tenants."""
    from flask import render_template
    with app.test_request_context("/portal/assignments"):
        g.user = {"sub": str(world["admin"]), "role": "admin",
                  "company": "dod", "email": ADMIN_EMAIL, "name": "Pytest Admin"}
        html = render_template("portal_assignments.html", jobs=[], is_admin=True)
    assert "<h1>Assignments</h1>" in html
    assert "Admin Assignments" not in html
    assert "Your Assignments" not in html
