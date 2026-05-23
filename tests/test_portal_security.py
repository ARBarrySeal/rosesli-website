"""Pytest suite for portal security hardening (P0/P1 wave-3).

Runs against the live local Postgres bot-db. COMPANY is bound to the COMPANY_ID
env var set in conftest, so this file is byte-identical between repos.

Coverage:
  * Admin MFA enforcement (the login_required enroll-state bug fix)
  * Account lockout after 5 failed attempts (incl. 423 response, counter reset)
  * JWT invalidation when pw_changed_at advances past the token's iat
  * portal_audit captures login_fail / login_ok / login_locked
"""
import os
import secrets
import time

import pytest

import portal_db
from portal_auth import hash_password


COMPANY = os.environ.get("COMPANY_ID", "dod")
TEST_EMAIL = "pytest-security@example.test"
TEST_PASSWORD = "PytestSecurity12345!"


def _cleanup():
    portal_db.execute("DELETE FROM portal_audit WHERE email = %s", (TEST_EMAIL,))
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = %s AND company = %s",
        (TEST_EMAIL, COMPANY),
    )


@pytest.fixture(autouse=True)
def _isolate_test_user():
    _cleanup()
    yield
    _cleanup()


@pytest.fixture
def csrf(client):
    with client.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        return sess["csrf_token"]


@pytest.fixture
def admin():
    portal_db.execute(
        "INSERT INTO portal_users (email, password_hash, full_name, role, company, "
        "                          active, mfa_secret) "
        "VALUES (%s, %s, 'Pytest Admin', 'admin', %s, TRUE, NULL)",
        (TEST_EMAIL, hash_password(TEST_PASSWORD), COMPANY),
    )
    return TEST_EMAIL


@pytest.fixture
def client_user():
    portal_db.execute(
        "INSERT INTO portal_users (email, password_hash, full_name, role, company, "
        "                          active) "
        "VALUES (%s, %s, 'Pytest Client', 'client', %s, TRUE)",
        (TEST_EMAIL, hash_password(TEST_PASSWORD), COMPANY),
    )
    return TEST_EMAIL


def _get_field(field):
    row = portal_db.query_one(
        f"SELECT {field} FROM portal_users WHERE email = %s AND company = %s",
        (TEST_EMAIL, COMPANY),
    )
    return row[field] if row else None


def _login(client, csrf, password=TEST_PASSWORD):
    return client.post(
        "/login",
        data={"email": TEST_EMAIL, "password": password, "csrf_token": csrf},
    )


# ─── Admin MFA enforcement (the P1 bug fix) ───────────────────────────────────

def test_admin_login_without_mfa_redirects_to_enroll(client, csrf, admin):
    r = _login(client, csrf)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["redirect"] == "/portal/mfa-enroll"
    assert "portal_token=" in r.headers.get("Set-Cookie", "")


def test_admin_with_enroll_cookie_can_reach_mfa_enroll_page(client, csrf, admin):
    _login(client, csrf)
    r = client.get("/portal/mfa-enroll", follow_redirects=False)
    assert r.status_code == 200
    assert b"<svg" in r.data  # QR code rendered


def test_admin_with_enroll_cookie_blocked_from_admin_routes(client, csrf, admin):
    """Critical: enroll-state admin must NOT bypass MFA via direct URL nav."""
    _login(client, csrf)
    r = client.get("/portal/admin/audit", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers.get("Location") == "/"


# ─── Account lockout (P1) ─────────────────────────────────────────────────────

def test_five_failed_attempts_lock_account(client, csrf, admin):
    for _ in range(5):
        r = _login(client, csrf, password="wrong")
        assert r.status_code == 401
    assert _get_field("failed_login_count") == 5
    assert _get_field("locked_until") is not None


def test_correct_password_during_lockout_returns_423(client, csrf, admin):
    for _ in range(5):
        _login(client, csrf, password="wrong")
    r = _login(client, csrf)
    assert r.status_code == 423


def test_successful_login_resets_failure_counter(client, csrf, admin):
    portal_db.execute(
        "UPDATE portal_users SET failed_login_count = 3 "
        "WHERE email = %s AND company = %s",
        (TEST_EMAIL, COMPANY),
    )
    assert _get_field("failed_login_count") == 3
    r = _login(client, csrf)
    assert r.status_code == 200
    assert _get_field("failed_login_count") == 0


# ─── JWT invalidation on password rotation (P1) ───────────────────────────────

def test_stale_jwt_rejected_after_password_change(client, csrf, client_user):
    r = _login(client, csrf)
    assert r.status_code == 200
    assert r.get_json()["redirect"] == "/portal"

    r = client.get("/portal/profile", follow_redirects=False)
    assert r.status_code == 200

    # Simulate the password being rotated AFTER the JWT was issued.
    time.sleep(1)
    portal_db.execute(
        "UPDATE portal_users SET pw_changed_at = NOW() + INTERVAL '5 seconds' "
        "WHERE email = %s AND company = %s",
        (TEST_EMAIL, COMPANY),
    )

    r = client.get("/portal/profile", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers.get("Location") == "/"


# ─── Audit log captures security events (P1) ──────────────────────────────────

def test_audit_log_captures_failed_and_successful_logins(client, csrf, client_user):
    _login(client, csrf, password="wrong")
    _login(client, csrf)
    rows = portal_db.query_all(
        "SELECT action FROM portal_audit WHERE email = %s ORDER BY id",
        (TEST_EMAIL,),
    )
    actions = [r["action"] for r in rows]
    assert "login_fail" in actions
    assert "login_ok" in actions


def test_audit_log_captures_account_lockout(client, csrf, admin):
    for _ in range(5):
        _login(client, csrf, password="wrong")
    _login(client, csrf)  # 6th attempt — should trip login_locked
    rows = portal_db.query_all(
        "SELECT action FROM portal_audit WHERE email = %s ORDER BY id",
        (TEST_EMAIL,),
    )
    actions = [r["action"] for r in rows]
    assert "login_fail" in actions
    assert "login_locked" in actions


# ─── P2c: CSRF token rotation on state transitions ────────────────────────────

def _current_csrf(client):
    with client.session_transaction() as sess:
        return sess.get("csrf_token")


def test_csrf_token_rotates_on_login(client, csrf, client_user):
    """A CSRF token observed pre-login must stop validating after login —
    blocks session-fixation where an attacker primes a victim's pre-auth
    session with a known token."""
    pre = csrf
    r = _login(client, csrf)
    assert r.status_code == 200
    post = _current_csrf(client)
    assert post is not None
    assert post != pre


def test_password_change_keeps_user_logged_in(client, csrf, client_user):
    """Wave-3 bug: the success message claimed 'you'll stay signed in here'
    but the pw_changed_at write invalidated this device's JWT too. Fix
    reissues a fresh JWT in the response."""
    _login(client, csrf)
    current = _current_csrf(client)

    new_pw = "BrandNewStrong456!"
    r = client.post(
        "/portal/profile",
        data={
            "action": "password",
            "current_password": TEST_PASSWORD,
            "new_password": new_pw,
            "confirm_password": new_pw,
            "csrf_token": current,
        },
    )
    assert r.status_code == 200
    assert b"Password updated" in r.data
    # New JWT must be set on the response.
    assert "portal_token=" in r.headers.get("Set-Cookie", "")
    # And the new cookie must keep this client authenticated.
    r2 = client.get("/portal/profile", follow_redirects=False)
    assert r2.status_code == 200


def test_password_change_rotates_csrf_token(client, csrf, client_user):
    _login(client, csrf)
    pre = _current_csrf(client)

    new_pw = "AnotherStrongOne789!"
    r = client.post(
        "/portal/profile",
        data={
            "action": "password",
            "current_password": TEST_PASSWORD,
            "new_password": new_pw,
            "confirm_password": new_pw,
            "csrf_token": pre,
        },
    )
    assert r.status_code == 200
    post = _current_csrf(client)
    assert post is not None
    assert post != pre


def test_password_change_invalidates_jwt_on_other_devices(client, csrf, client_user, app):
    """Same user logs in from device A, changes password from device B —
    device A's next request must be rejected (verifies the cross-device
    invalidation half of the design)."""
    # Device A logs in (cookies live in `client`).
    _login(client, csrf)

    # JWT iat is integer-seconds precision. If device B's pw_changed_at write
    # lands in the same wall-clock second as device A's JWT iat, the strict
    # `iat < pw_changed_at` check would erroneously treat device A's token
    # as valid. Sleep to push device B's timestamps a full second forward.
    time.sleep(1.1)

    # Device B is a separate test client; logs in and changes password.
    device_b = app.test_client()
    with device_b.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        b_csrf = sess["csrf_token"]
    rb = device_b.post(
        "/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "csrf_token": b_csrf},
    )
    assert rb.status_code == 200
    with device_b.session_transaction() as sess:
        b_current = sess["csrf_token"]
    new_pw = "ChangedFromDeviceB!9"
    rc = device_b.post(
        "/portal/profile",
        data={
            "action": "password",
            "current_password": TEST_PASSWORD,
            "new_password": new_pw,
            "confirm_password": new_pw,
            "csrf_token": b_current,
        },
    )
    assert rc.status_code == 200

    # Exercise device A's now-stale JWT.
    ra = client.get("/portal/profile", follow_redirects=False)
    assert ra.status_code == 302
    assert ra.headers.get("Location") == "/"
