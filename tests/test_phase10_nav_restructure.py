"""Phase 10 (2026-07-22 batch) — Main menu / nav restructure. LAST phase in
the batch; depends on every page built in Phases 1-9 existing already.

Covers:
  * 10.1/10.2 "Create Interpreter Invoice" / "Create Client Invoice" nav
    links are gone — but the routes themselves still work (reachable via
    the "+ New Invoice" buttons already on their list pages, per the
    doc's own stated fallback)
  * 10.3 "Client Review" nav link exists (was already added in Phase 2)
  * 10.4 "Admin Assignments" is the first item in the Admin section
  * 10.5 "Invite User" nav link is gone — /portal/admin/invite route still
    works (reachable via the "+ Invite User" button already on Users)
  * 10.6 "Availability" sits directly after "Profile"
  * 10.7 "Interpreter Invoices" (near "Incoming Requests"), "Client
    Invoices", and "Profile" all moved into the Admin section
  * dod tenant: rosesli-only nav items stay gated off (no regression)
"""
import os
import re
import secrets

import pytest
from flask import g, render_template

import portal_db
from portal_auth import hash_password

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase10_12345!"

ADMIN_EMAIL = "pytest-p10-admin@example.test"
EMAILS = [ADMIN_EMAIL]


def _cleanup():
    portal_db.execute("DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY))
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))


@pytest.fixture
def world():
    _cleanup()
    row = portal_db.execute(
        "INSERT INTO portal_users (email, password_hash, full_name, role, company, active) "
        "VALUES (%s, %s, %s, 'admin', %s, TRUE) RETURNING id",
        (ADMIN_EMAIL, hash_password(PW), "Pytest Admin", COMPANY),
    )
    yield {"admin": row["id"]}
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


def _nav_order(html, labels):
    """Positions (index into `html`) of each label's first occurrence, in
    the order given — asserts every label is present and returns positions
    for ordering checks."""
    positions = []
    for label in labels:
        idx = html.find(label)
        assert idx != -1, f"{label!r} not found in nav"
        positions.append(idx)
    return positions


# ── 10.1 / 10.2 — standalone create-invoice links removed, routes intact ───

def test_create_invoice_nav_links_removed(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal").data.decode()
    assert "Create Interpreter Invoice" not in html
    assert "Create Client Invoice" not in html


def test_create_invoice_routes_still_reachable(app, world):
    admin = _client(app, ADMIN_EMAIL)
    assert admin.get("/portal/admin/invoices/create").status_code == 200
    assert admin.get("/portal/admin/client-invoices/create").status_code == 200


def test_new_invoice_buttons_still_on_list_pages(app, world):
    admin = _client(app, ADMIN_EMAIL)
    assert "/portal/admin/invoices/create" in admin.get("/portal/invoices").data.decode()
    assert "/portal/admin/client-invoices/create" in admin.get("/portal/client-invoices").data.decode()


# ── 10.3 — Client Review link exists ────────────────────────────────────────

def test_client_review_nav_link_present(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal").data.decode()
    assert 'href="/portal/admin/client-review"' in html


# ── 10.4 — Admin Assignments is first in the Admin section ─────────────────

def test_admin_assignments_leads_the_admin_section(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal").data.decode()
    admin_section = html.split('sidebar-label">Admin<', 1)[1]
    first_link = re.search(r'href="([^"]+)"', admin_section)
    assert first_link.group(1) == "/portal/assignments"


# ── 10.5 — Invite User link removed, route intact ───────────────────────────

def test_invite_user_nav_link_removed(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal").data.decode()
    assert "Invite User" not in html


def test_invite_route_still_reachable_from_users_page(app, world):
    admin = _client(app, ADMIN_EMAIL)
    assert admin.get("/portal/admin/invite").status_code == 200
    assert "/portal/admin/invite" in admin.get("/portal/admin/users").data.decode()


# ── 10.6 — Availability sits directly after Profile ─────────────────────────

def test_availability_follows_profile(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal").data.decode()
    profile_pos, avail_pos = _nav_order(html, ["Profile</a>", "Availability</a>"])
    assert profile_pos < avail_pos
    # Nothing else with an href should sit between them.
    between = html[profile_pos:avail_pos]
    assert between.count("href=") == 1  # just Availability's own link


# ── 10.7 — Interpreter Invoices / Client Invoices / Profile in Admin section ─

def test_interpreter_invoices_client_invoices_and_profile_in_admin_section(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal").data.decode()
    admin_section = html.split('sidebar-label">Admin<', 1)[1]
    assert "Interpreter Invoices</a>" in admin_section
    assert "Client Invoices</a>" in admin_section
    assert "Profile</a>" in admin_section


def test_interpreter_invoices_near_incoming_requests(app, world):
    admin = _client(app, ADMIN_EMAIL)
    html = admin.get("/portal").data.decode()
    ir_pos, ii_pos = _nav_order(html, ["Incoming Requests</a>", "Interpreter Invoices</a>"])
    assert ir_pos < ii_pos
    between = html[ir_pos:ii_pos]
    assert between.count("href=") == 1  # just Interpreter Invoices' own link


# ── dod tenant unaffected: rosesli-only items stay gated off ───────────────

def test_dod_nav_keeps_rosesli_only_items_gated(app, world):
    with app.test_request_context("/portal/documents"):
        g.user = {"sub": str(world["admin"]), "role": "admin",
                  "company": "dod", "email": ADMIN_EMAIL, "name": "Pytest Admin"}
        html = render_template("portal_documents.html", documents=[])
    assert "Admin Assignments" not in html
    assert "Job Offers" not in html
    assert "Interpreter Review" not in html
    assert "Client Review" not in html
    assert "Calendar</a>" not in html
    assert "Differentials</a>" not in html
    assert "Availability</a>" not in html
    # Universal items still present for dod.
    assert "Interpreter Invoices</a>" in html
    assert "Client Invoices</a>" in html
    assert "Profile</a>" in html
