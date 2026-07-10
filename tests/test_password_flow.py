"""Forced-password-change lifecycle (migration 013).

Runs against the live local Postgres bot-db, same pattern as
test_portal_security.py. rosesli-specific flows are skipped when the repo
runs as another company.

Coverage:
  * generate_temp_password satisfies both tenants' policies
  * login with the literal default password forces the change flow
  * must_change_password gate: every portal page redirects to the change form
  * the change form clears the flag, rejects weak/mismatched passwords
  * forgot-password (rosesli) issues a temp password + sets the flag
  * admin "Issue temporary password" from the profile admin-view
"""
import os
import secrets

import pytest

import portal_db
from portal_auth import (
    DEFAULT_PASSWORD, check_password, generate_temp_password, hash_password,
)

COMPANY = os.environ.get("COMPANY_ID", "dod")
ROSESLI_ONLY = pytest.mark.skipif(COMPANY != "rosesli",
                                  reason="rosesli-only password flow")

USER_EMAIL  = "pytest-pwflow@example.test"
ADMIN_EMAIL = "pytest-pwflow-admin@example.test"
GOOD_PW     = "Brand-New1!"        # passes both policies? rosesli yes; only used as new pw
ADMIN_PW    = "PytestPwFlow12345!"


def _cleanup():
    for email in (USER_EMAIL, ADMIN_EMAIL):
        portal_db.execute("DELETE FROM portal_audit WHERE email = %s", (email,))
        portal_db.execute(
            "DELETE FROM portal_users WHERE email = %s AND company = %s",
            (email, COMPANY),
        )


@pytest.fixture(autouse=True)
def _isolate():
    _cleanup()
    yield
    _cleanup()


@pytest.fixture
def csrf(client):
    with client.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        return sess["csrf_token"]


def _make_user(password=DEFAULT_PASSWORD, must_change=False, role="employee",
               email=USER_EMAIL):
    portal_db.execute(
        "INSERT INTO portal_users (email, password_hash, full_name, role, company, "
        "                          active, must_change_password) "
        "VALUES (%s, %s, 'Pytest PwFlow', %s, %s, TRUE, %s)",
        (email, hash_password(password), role, COMPANY, must_change),
    )
    row = portal_db.query_one(
        "SELECT id FROM portal_users WHERE email = %s AND company = %s",
        (email, COMPANY),
    )
    return row["id"]


def _get(field, email=USER_EMAIL):
    row = portal_db.query_one(
        f"SELECT {field} FROM portal_users WHERE email = %s AND company = %s",
        (email, COMPANY),
    )
    return row[field] if row else None


def _login(client, csrf, email=USER_EMAIL, password=DEFAULT_PASSWORD):
    return client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf},
    )


def _session_csrf(client):
    with client.session_transaction() as sess:
        return sess.get("csrf_token")


# ─── Temp-password generator ─────────────────────────────────────────────────


def test_temp_password_meets_both_policies(monkeypatch):
    from portal_auth import _password_too_weak
    for _ in range(25):
        pw = generate_temp_password()
        monkeypatch.setenv("COMPANY_ID", "rosesli")
        assert _password_too_weak(pw) is None, pw
        monkeypatch.setenv("COMPANY_ID", "dod")
        assert _password_too_weak(pw) is None, pw


def test_temp_passwords_are_unique():
    batch = {generate_temp_password() for _ in range(50)}
    assert len(batch) == 50


# ─── Default-password sentinel at login ──────────────────────────────────────


def test_login_with_default_password_forces_change(client, csrf):
    _make_user(password=DEFAULT_PASSWORD, must_change=False)  # flag NOT set
    r = _login(client, csrf)
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["redirect"] == "/portal/change-password"
    assert _get("must_change_password") is True  # sentinel back-filled the flag


def test_login_with_flag_set_forces_change(client, csrf):
    _make_user(password=GOOD_PW, must_change=True)
    r = _login(client, csrf, password=GOOD_PW)
    assert r.get_json()["redirect"] == "/portal/change-password"


def test_normal_login_unaffected(client, csrf):
    _make_user(password=GOOD_PW, must_change=False)
    r = _login(client, csrf, password=GOOD_PW)
    assert r.get_json()["redirect"] == "/portal"


# ─── The gate: flagged users can't reach anything but the change form ────────


def test_flagged_user_redirected_from_portal_pages(client, csrf):
    _make_user(must_change=True)
    _login(client, csrf)
    for path in ("/portal", "/portal/profile", "/portal/invoices"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 302, path
        assert r.headers["Location"] == "/portal/change-password", path


def test_flagged_user_can_load_change_form(client, csrf):
    _make_user(must_change=True)
    _login(client, csrf)
    r = client.get("/portal/change-password")
    assert r.status_code == 200
    assert b"new password" in r.data.lower()


def test_unflagged_user_bounced_off_change_form(client, csrf):
    _make_user(password=GOOD_PW, must_change=False)
    _login(client, csrf, password=GOOD_PW)
    r = client.get("/portal/change-password", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"] == "/portal"


# ─── Completing the forced change ────────────────────────────────────────────


@ROSESLI_ONLY
def test_change_password_clears_flag_and_unlocks_portal(client, csrf):
    _make_user(must_change=True)
    _login(client, csrf)
    token = _session_csrf(client)
    r = client.post("/portal/change-password", data={
        "new_password": GOOD_PW, "confirm_password": GOOD_PW, "csrf_token": token,
    }, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["Location"] == "/portal"
    assert _get("must_change_password") is False
    assert check_password(GOOD_PW, _get("password_hash"))
    # And the session keeps working (fresh JWT was issued).
    r2 = client.get("/portal", follow_redirects=False)
    assert r2.status_code == 200


@ROSESLI_ONLY
def test_change_password_rejects_mismatch(client, csrf):
    _make_user(must_change=True)
    _login(client, csrf)
    token = _session_csrf(client)
    r = client.post("/portal/change-password", data={
        "new_password": GOOD_PW, "confirm_password": GOOD_PW + "x",
        "csrf_token": token,
    })
    assert r.status_code == 200
    assert b"do not match" in r.data
    assert _get("must_change_password") is True


@ROSESLI_ONLY
def test_change_password_rejects_weak(client, csrf):
    _make_user(must_change=True)
    _login(client, csrf)
    token = _session_csrf(client)
    r = client.post("/portal/change-password", data={
        "new_password": "abc", "confirm_password": "abc", "csrf_token": token,
    })
    assert r.status_code == 200
    assert _get("must_change_password") is True


@ROSESLI_ONLY
def test_change_password_rejects_default_password(client, csrf):
    _make_user(must_change=True)
    _login(client, csrf)
    token = _session_csrf(client)
    r = client.post("/portal/change-password", data={
        "new_password": DEFAULT_PASSWORD, "confirm_password": DEFAULT_PASSWORD,
        "csrf_token": token,
    })
    assert r.status_code == 200
    assert _get("must_change_password") is True


# ─── Forgot-password issues a temp password (rosesli) ────────────────────────


@ROSESLI_ONLY
def test_forgot_password_issues_temp_and_sets_flag(client, csrf):
    _make_user(password=GOOD_PW, must_change=False)
    old_hash = _get("password_hash")
    r = client.post("/forgot-password",
                    data={"email": USER_EMAIL, "csrf_token": csrf})
    assert r.status_code == 200
    assert _get("must_change_password") is True
    assert _get("password_hash") != old_hash        # temp password installed
    assert _get("reset_token") is None              # token flow not used


@ROSESLI_ONLY
def test_forgot_password_unknown_email_no_leak(client, csrf):
    r = client.post("/forgot-password",
                    data={"email": "nobody-here@example.test", "csrf_token": csrf})
    assert r.status_code == 200  # same page either way — no user enumeration


# ─── Admin-issued temporary password ─────────────────────────────────────────


@ROSESLI_ONLY
def test_admin_can_issue_temp_password(client, csrf):
    target_id = _make_user(password=GOOD_PW, must_change=False)
    _make_user(password=ADMIN_PW, role="admin", email=ADMIN_EMAIL)
    old_hash = _get("password_hash")

    r = _login(client, csrf, email=ADMIN_EMAIL, password=ADMIN_PW)
    assert r.get_json()["ok"] is True
    token = _session_csrf(client)

    r = client.post("/portal/profile", data={
        "action": "set_temp_password", "view_uid": str(target_id),
        "csrf_token": token,
    })
    assert r.status_code == 200
    assert b"Temporary password issued" in r.data
    assert _get("must_change_password") is True
    assert _get("password_hash") != old_hash


def test_non_admin_cannot_issue_temp_password(client, csrf):
    # A non-admin posting the action against their own profile is a no-op
    # (is_admin_view is False), and they can't target other users at all.
    victim_id = _make_user(password=GOOD_PW, email=ADMIN_EMAIL, role="client")
    _make_user(password=GOOD_PW, must_change=False)
    _login(client, csrf, password=GOOD_PW)
    token = _session_csrf(client)
    old_hash = _get("password_hash", email=ADMIN_EMAIL)

    r = client.post("/portal/profile", data={
        "action": "set_temp_password", "view_uid": str(victim_id),
        "csrf_token": token,
    })
    assert r.status_code == 200
    assert _get("password_hash", email=ADMIN_EMAIL) == old_hash
    assert _get("must_change_password", email=ADMIN_EMAIL) is False
