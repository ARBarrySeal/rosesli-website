"""Tests for SMTP send-test feature (P3).

Two layers:
  * Unit tests on portal_email.send_test_email (no auth, mock smtplib)
  * Route tests on /portal/admin/smtp-test (admin auth required, audit logged)
"""
import os
import secrets

import pytest

import portal_db
import portal_email
from portal_auth import encode_jwt, hash_password


COMPANY = os.environ.get("COMPANY_ID", "dod")
TEST_EMAIL = "pytest-smtp@example.test"


def _cleanup():
    portal_db.execute(
        "DELETE FROM portal_audit WHERE email = %s OR metadata::text LIKE %s",
        (TEST_EMAIL, f'%{TEST_EMAIL}%'),
    )
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = %s AND company = %s",
        (TEST_EMAIL, COMPANY),
    )


@pytest.fixture(autouse=True)
def _isolate():
    _cleanup()
    yield
    _cleanup()


# ─── Unit tests on send_test_email ────────────────────────────────────────────

def test_send_test_email_returns_not_configured_when_env_unset(monkeypatch):
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "RESEND_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    ok, detail = portal_email.send_test_email("a@b.test", "TestCo")
    assert ok is False
    assert "not configured" in detail.lower()


def test_send_test_email_success_with_mock(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "user@example.test")
    monkeypatch.setenv("SMTP_PASS", "pw")

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **kw): pass
        def login(self, u, p): pass
        def send_message(self, msg): self.sent_to = msg["To"]

    monkeypatch.setattr(portal_email.smtplib, "SMTP", FakeSMTP)
    ok, detail = portal_email.send_test_email("admin@example.test", "TestCo")
    assert ok is True
    assert "smtp.example.test" in detail


def test_send_test_email_surfaces_auth_failure(monkeypatch):
    import smtplib as _smtplib
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "user@example.test")
    monkeypatch.setenv("SMTP_PASS", "wrong")

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **kw): pass
        def login(self, u, p):
            raise _smtplib.SMTPAuthenticationError(535, b"5.7.8 wrong password")
        def send_message(self, msg): pass

    monkeypatch.setattr(portal_email.smtplib, "SMTP", FakeSMTP)
    ok, detail = portal_email.send_test_email("admin@example.test", "TestCo")
    assert ok is False
    assert "auth failed" in detail.lower()
    assert "535" in detail
    assert "wrong password" in detail


def test_send_test_email_surfaces_connection_error(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "user@example.test")
    monkeypatch.setenv("SMTP_PASS", "pw")

    def boom(*a, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr(portal_email.smtplib, "SMTP", boom)
    ok, detail = portal_email.send_test_email("admin@example.test", "TestCo")
    assert ok is False
    assert "connection" in detail.lower()
    assert "refused" in detail.lower()


# ─── Route tests on /portal/admin/smtp-test ───────────────────────────────────

def _create_admin():
    portal_db.execute(
        "INSERT INTO portal_users (email, password_hash, full_name, role, company, "
        "                          active, mfa_secret) "
        "VALUES (%s, %s, 'Pytest Admin', 'admin', %s, TRUE, 'fake-secret') RETURNING id",
        ("pytest-smtp@example.test", hash_password("strongpass"), COMPANY),
    )
    row = portal_db.query_one(
        "SELECT id FROM portal_users WHERE email = %s AND company = %s",
        (TEST_EMAIL, COMPANY),
    )
    return row["id"]


def _inject_admin_session(client, app, uid):
    """Bypass the full login + MFA flow by issuing a 'mfa=ok' JWT directly."""
    with app.app_context():
        token = encode_jwt({
            "sub":     str(uid),
            "email":   TEST_EMAIL,
            "role":    "admin",
            "company": COMPANY,
            "name":    "Pytest Admin",
            "mfa":     "ok",
        })
    client.set_cookie("portal_token", token, domain="localhost")
    with client.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        return sess["csrf_token"]


def test_smtp_test_requires_authenticated_admin(client):
    """Unauthenticated POST -> redirect to / (login_required)."""
    r = client.post("/portal/admin/smtp-test", follow_redirects=False)
    # Will fail CSRF first (403) or auth (302) — either way it's NOT 200.
    assert r.status_code in (302, 403)


def test_smtp_test_route_returns_json_and_logs_audit(client, app, monkeypatch):
    uid = _create_admin()
    csrf = _inject_admin_session(client, app, uid)

    # Force SMTP unconfigured -> route still returns 200 with ok=False.
    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "RESEND_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    r = client.post(
        "/portal/admin/smtp-test",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is False
    assert body["sent_to"] == TEST_EMAIL
    assert "not configured" in body["detail"].lower()

    # Audit row written.
    row = portal_db.query_one(
        "SELECT action, metadata FROM portal_audit "
        "WHERE action = 'smtp_test' ORDER BY id DESC LIMIT 1",
    )
    assert row is not None
    assert row["action"] == "smtp_test"
    assert row["metadata"]["to"] == TEST_EMAIL
    assert row["metadata"]["ok"] is False


def test_smtp_test_route_succeeds_with_mocked_smtp(client, app, monkeypatch):
    uid = _create_admin()
    csrf = _inject_admin_session(client, app, uid)

    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "user@example.test")
    monkeypatch.setenv("SMTP_PASS", "pw")

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **kw): pass
        def login(self, u, p): pass
        def send_message(self, msg): pass

    monkeypatch.setattr(portal_email.smtplib, "SMTP", FakeSMTP)

    r = client.post(
        "/portal/admin/smtp-test",
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["sent_to"] == TEST_EMAIL
    assert "smtp.example.test" in body["detail"]
