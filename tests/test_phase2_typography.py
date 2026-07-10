"""Phase 2 — 18pt typography rescale + navigation buttons (Rose SLI).

Covers:
  * portal.css carries the rosesli root rescale rule (24px = 18pt) and black text,
    gated on data-company so the dod theme is untouched
  * logged-in portal pages stamp data-company on <html> and <body>
  * client invoice pages show See Requests / Back to Dashboard buttons for
    rosesli clients only
  * admin pages (not the dashboard itself) show an Exit to Dashboard button
  * standalone auth pages (forgot-password) stamp the tenant's data-company,
    including when the tenant is dod
"""
import os
import secrets

import pytest

import portal_db
from portal_auth import hash_password


COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase2_12345!"

ADMIN_EMAIL = "pytest-p2-admin@example.test"
CLIENT_EMAIL = "pytest-p2-client@example.test"
INT_EMAIL = "pytest-p2-int@example.test"
EMAILS = [ADMIN_EMAIL, CLIENT_EMAIL, INT_EMAIL]

CSS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "portal.css")


# ── Setup / teardown ──────────────────────────────────────────────────────────

def _cleanup():
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY),
    )
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))


def _mk_user(email, role, **cols):
    base = "INSERT INTO portal_users (email, password_hash, full_name, role, company, active"
    vals = [email, hash_password(PW), cols.pop("full_name", f"Pytest {role.title()}"), role, COMPANY, True]
    extra = ""
    for k, v in cols.items():
        extra += f", {k}"
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    row = portal_db.execute(
        f"{base}{extra}) VALUES ({placeholders}) RETURNING id", tuple(vals),
    )
    return row["id"]


@pytest.fixture
def world():
    _cleanup()
    admin_id = _mk_user(ADMIN_EMAIL, "admin")
    client_id = _mk_user(CLIENT_EMAIL, "client")
    int_id = _mk_user(INT_EMAIL, "employee", full_name="Casey Terp", interpreter_rate=80)
    yield {"admin": admin_id, "client": client_id, "int": int_id}
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


# ── 1. Stylesheet rescale rule ────────────────────────────────────────────────

def test_css_has_rosesli_root_rescale():
    with open(CSS_PATH, encoding="utf-8") as f:
        css = f.read()
    assert 'html[data-company="rosesli"]' in css
    rule = css.split('html[data-company="rosesli"]', 1)[1].split("}", 1)[0]
    assert "font-size: 24px" in rule


def test_css_rosesli_text_is_black_headings_navy():
    with open(CSS_PATH, encoding="utf-8") as f:
        css = f.read()
    body_block = css.split('body[data-company="rosesli"] {', 1)[1].split("}", 1)[0]
    assert "#000000" in body_block
    heading_rule = css.split('body[data-company="rosesli"] .portal-main h1', 1)[1].split("}", 1)[0]
    assert "#0d2b52" in heading_rule


# ── 2. Portal pages stamp data-company ────────────────────────────────────────

def test_portal_page_carries_company_attr(app, world):
    c = _client(app, CLIENT_EMAIL)
    html = c.get("/portal/client-invoices").data.decode()
    assert f'<html lang="en" data-company="{COMPANY}">' in html
    assert f'<body data-company="{COMPANY}">' in html


# ── 3. Client invoice nav buttons ─────────────────────────────────────────────

def test_client_invoice_list_buttons_for_rosesli_client(app, world):
    html = _client(app, CLIENT_EMAIL).get("/portal/client-invoices").data.decode()
    assert "See Requests" in html
    assert "Back to Dashboard" in html


def test_client_invoice_buttons_hidden_from_interpreter(app, world):
    r = _client(app, INT_EMAIL).get("/portal/requests")
    assert "See Requests" not in r.data.decode()


# ── 4. Admin Exit to Dashboard ────────────────────────────────────────────────

def test_admin_exit_to_dashboard_on_subpages_not_dashboard(app, world):
    c = _client(app, ADMIN_EMAIL)
    assert "Exit to Dashboard" in c.get("/portal/requests").data.decode()
    assert "Exit to Dashboard" not in c.get("/portal").data.decode()


def test_no_exit_button_for_client(app, world):
    html = _client(app, CLIENT_EMAIL).get("/portal/requests").data.decode()
    assert "Exit to Dashboard" not in html


# ── 5. Standalone auth pages ──────────────────────────────────────────────────

def test_forgot_password_stamps_company(app):
    html = app.test_client().get("/forgot-password").data.decode()
    assert f'data-company="{COMPANY}"' in html


def test_forgot_password_stamps_dod_when_dod_tenant(app, monkeypatch):
    monkeypatch.setenv("COMPANY_ID", "dod")
    html = app.test_client().get("/forgot-password").data.decode()
    assert 'data-company="dod"' in html
    assert 'data-company="rosesli"' not in html
