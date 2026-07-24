"""Phase 7 (2026-07-22 batch) — Job Offers visibility.

The /portal/offers guard (admin + employee only, explicit abort(403) for
everyone else) and the nav (no "Job Offers" link in the client menu) already
existed before this batch. This phase is pure verification: adds the
explicit test coverage the doc calls for, with no code changes needed.
"""
import os
import secrets

import pytest

import portal_db
from portal_auth import hash_password

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase7_12345!"

ADMIN_EMAIL = "pytest-p7v-admin@example.test"
EMP_EMAIL = "pytest-p7v-emp@example.test"
CLIENT_EMAIL = "pytest-p7v-client@example.test"
EMAILS = [ADMIN_EMAIL, EMP_EMAIL, CLIENT_EMAIL]


def _cleanup():
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
    _mk_user(ADMIN_EMAIL, "admin")
    _mk_user(EMP_EMAIL, "employee")
    _mk_user(CLIENT_EMAIL, "client")
    yield
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


def test_client_forbidden_from_job_offers_page(app, world):
    c = _client(app, CLIENT_EMAIL)
    r = c.get("/portal/offers")
    assert r.status_code == 403


def test_admin_can_view_job_offers_page(app, world):
    c = _client(app, ADMIN_EMAIL)
    r = c.get("/portal/offers")
    assert r.status_code == 200


def test_employee_can_view_job_offers_page(app, world):
    c = _client(app, EMP_EMAIL)
    r = c.get("/portal/offers")
    assert r.status_code == 200


def test_client_nav_has_no_job_offers_link(app, world):
    c = _client(app, CLIENT_EMAIL)
    html = c.get("/portal").data.decode()
    assert "Job Offers" not in html
    assert 'href="/portal/offers"' not in html


def test_admin_and_employee_nav_have_job_offers_link(app, world):
    admin_html = _client(app, ADMIN_EMAIL).get("/portal").data.decode()
    emp_html = _client(app, EMP_EMAIL).get("/portal").data.decode()
    assert 'href="/portal/offers"' in admin_html
    assert 'href="/portal/offers"' in emp_html
