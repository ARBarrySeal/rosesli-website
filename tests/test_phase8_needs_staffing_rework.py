"""Phase 8 (2026-07-22 batch) — "Needs staffing" interpreter-facing rework.

Covers the genuinely-new Phase 8 work (8.1/8.2 were already true of the
existing /portal/offers pending table — no client column, date + start-end
time already shown):

  * 8.3 pending-offers table shows zip only, never the full street address
  * 8.4 pending-offers table shows the assignment's Type
  * 8.6 accepting sends the interpreter themselves a confirmation email
    (client name, interpreters assigned/"Unassigned", notes, doc names) and
    grants them read access to the assignment detail page before they're
    formally confirmed/staffed
  * 8.7 the admin detail page shows an "accepted, awaiting confirm" line for
    an accepted-but-not-yet-staffed interpreter (display only — Charles
    confirmed staffing still requires the separate manual Confirm click)
  * 8.8 declining greys out that interpreter's option in THIS assignment's
    edit-form dropdown only, not on other assignments
"""
import os
import secrets
import datetime as dt

import pytest

import portal_db
import portal_email
from portal_auth import hash_password
from portal_offers import has_open_or_accepted_offer

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase8_12345!"

ADMIN_EMAIL = "pytest-p8-admin@example.test"
INT1_EMAIL = "pytest-p8-int1@example.test"
INT2_EMAIL = "pytest-p8-int2@example.test"
EMAILS = [ADMIN_EMAIL, INT1_EMAIL, INT2_EMAIL]

JOB_MARKER = "pytest-p8-job-marker"
EVENT_DATE = dt.date.today() + dt.timedelta(days=50)


def _cleanup():
    portal_db.execute(
        "DELETE FROM jobs WHERE (notes = %s OR interpreter_notes = %s) AND company = %s",
        (JOB_MARKER, JOB_MARKER, COMPANY),
    )
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
    job = portal_db.execute(
        "INSERT INTO jobs (company, status, event_date, num_interpreters, notes, "
        "  event_address, event_zip, assignment_type, client_name, interpreter_notes) "
        "VALUES (%s, 'pending', %s, 1, %s, %s, %s, %s, %s, %s) RETURNING id",
        (COMPANY, EVENT_DATE, JOB_MARKER, "123 Secret St, San Diego", "92101",
         "Legal", "Acme Corp", "Bring your own headset"),
    )
    yield {"admin": admin_id, "int1": int1_id, "int2": int2_id, "job": job["id"]}
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


def _offer_id(job_id, interpreter_id):
    return portal_db.query_one(
        "SELECT id, status FROM job_offers WHERE job_id = %s AND interpreter_id = %s AND company = %s",
        (job_id, interpreter_id, COMPANY),
    )


def _offer(app, admin, job_id, interpreter_id):
    admin.post(f"/portal/admin/assignments/{job_id}/offer",
              data={"interpreter_id": interpreter_id, "csrf_token": _csrf(admin)})
    return _offer_id(job_id, interpreter_id)


# ── 1. Pending-offers table: zip only, no full address, shows Type ─────────

def test_pending_offers_table_shows_zip_not_full_address(app, world):
    _offer(app, _client(app, ADMIN_EMAIL), world["job"], world["int1"])
    intp = _client(app, INT1_EMAIL)
    html = intp.get("/portal/offers").data.decode()
    assert "92101" in html
    assert "123 Secret St" not in html
    assert "Secret" not in html


def test_pending_offers_table_shows_type(app, world):
    _offer(app, _client(app, ADMIN_EMAIL), world["job"], world["int1"])
    intp = _client(app, INT1_EMAIL)
    html = intp.get("/portal/offers").data.decode()
    assert "Legal" in html


def test_pending_offers_table_has_no_client_column(app, world):
    _offer(app, _client(app, ADMIN_EMAIL), world["job"], world["int1"])
    intp = _client(app, INT1_EMAIL)
    html = intp.get("/portal/offers").data.decode()
    assert "Acme Corp" not in html


# ── 2. has_open_or_accepted_offer + detail-page access before confirm ──────

def test_has_open_or_accepted_offer_reflects_status(app, world):
    admin = _client(app, ADMIN_EMAIL)
    assert has_open_or_accepted_offer(world["job"], COMPANY, world["int1"]) is False
    _offer(app, admin, world["job"], world["int1"])
    assert has_open_or_accepted_offer(world["job"], COMPANY, world["int1"]) is True

    offer = _offer_id(world["job"], world["int1"])
    intp = _client(app, INT1_EMAIL)
    intp.post(f"/portal/offers/{offer['id']}/accept", data={"csrf_token": _csrf(intp)})
    assert has_open_or_accepted_offer(world["job"], COMPANY, world["int1"]) is True


def test_accepted_interpreter_can_view_detail_before_confirm(app, world):
    admin = _client(app, ADMIN_EMAIL)
    offer = _offer(app, admin, world["job"], world["int1"])
    intp = _client(app, INT1_EMAIL)
    intp.post(f"/portal/offers/{offer['id']}/accept", data={"csrf_token": _csrf(intp)})

    r = intp.get(f"/portal/assignments/{world['job']}")
    assert r.status_code == 200


def test_unrelated_interpreter_still_forbidden_from_detail(app, world):
    c = _client(app, INT2_EMAIL)
    r = c.get(f"/portal/assignments/{world['job']}")
    assert r.status_code == 403


# ── 3. Admin detail page shows "pending confirm" line ──────────────────────

def test_admin_detail_shows_pending_confirm_line(app, world):
    admin = _client(app, ADMIN_EMAIL)
    offer = _offer(app, admin, world["job"], world["int1"])
    intp = _client(app, INT1_EMAIL)
    intp.post(f"/portal/offers/{offer['id']}/accept", data={"csrf_token": _csrf(intp)})

    html = admin.get(f"/portal/assignments/{world['job']}").data.decode()
    assert "awaiting confirm" in html
    # Job is still pending — not silently auto-staffed.
    job = portal_db.query_one("SELECT status FROM jobs WHERE id = %s", (world["job"],))
    assert job["status"] == "pending"


# ── 4. Accept sends a confirmation email to the interpreter themselves ─────

def test_accept_sends_confirmation_email_to_interpreter(app, world, monkeypatch):
    captured = {}

    def fake_send(to_email, to_name, job, staffed_names, doc_names, portal_url, company_name):
        captured["to_email"] = to_email
        captured["staffed_names"] = staffed_names
        captured["client"] = job.get("client_name")
        return True

    monkeypatch.setattr(portal_email, "send_offer_accepted_confirmation_email", fake_send)
    # portal_offers imported the function directly — patch that binding too.
    import portal_offers
    monkeypatch.setattr(portal_offers.portal_email, "send_offer_accepted_confirmation_email", fake_send)

    admin = _client(app, ADMIN_EMAIL)
    offer = _offer(app, admin, world["job"], world["int1"])
    intp = _client(app, INT1_EMAIL)
    intp.post(f"/portal/offers/{offer['id']}/accept", data={"csrf_token": _csrf(intp)})

    assert captured.get("to_email") == INT1_EMAIL
    assert captured.get("staffed_names") == []  # nobody formally staffed yet
    assert captured.get("client") == "Acme Corp"


# ── 5. Decline greys out that interpreter for THIS assignment only ─────────

def test_decline_disables_option_on_this_assignment_only(app, world):
    admin = _client(app, ADMIN_EMAIL)
    offer = _offer(app, admin, world["job"], world["int1"])
    intp = _client(app, INT1_EMAIL)
    intp.post(f"/portal/offers/{offer['id']}/decline", data={"csrf_token": _csrf(intp)})

    html = admin.get(f"/portal/admin/assignments/{world['job']}/edit").data.decode()
    assert "declined this job" in html

    other_job = portal_db.execute(
        "INSERT INTO jobs (company, status, event_date, notes) VALUES (%s, 'pending', %s, %s) RETURNING id",
        (COMPANY, EVENT_DATE, JOB_MARKER),
    )["id"]
    other_html = admin.get(f"/portal/admin/assignments/{other_job}/edit").data.decode()
    assert "declined this job" not in other_html
