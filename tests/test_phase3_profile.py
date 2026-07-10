"""Phase 3 — profile restructure (Rose SLI).

Covers:
  * migration 014 backfill: full_name → first/last/middle_initial split
    (rosesli rows only; re-runnable)
  * full_name recomposed "First M. Last" on every rosesli profile save
  * base rate ignored on non-admin POST (self-edit closed) but saved on
    admin-view POST
  * legacy address column untouched by rosesli saves; split fields win
  * off-list certification/specialty values round-trip unchanged
  * admin list pages render "Last, First"
"""
import os
import secrets

import pytest

import portal_db
from portal_auth import hash_password


COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase3_12345!"

ADMIN_EMAIL = "pytest-p3-admin@example.test"
INT_EMAIL = "pytest-p3-int@example.test"
CLIENT_EMAIL = "pytest-p3-client@example.test"
EMAILS = [ADMIN_EMAIL, INT_EMAIL, CLIENT_EMAIL]

MIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "migrations", "014_profile_fields.sql")


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
    ids = {
        "admin": _mk_user(ADMIN_EMAIL, "admin"),
        "int": _mk_user(INT_EMAIL, "employee", full_name="Casey Q. Terp",
                        interpreter_rate=80, address="1 Legacy Way, Old Town"),
        "client": _mk_user(CLIENT_EMAIL, "client", full_name="Client Person",
                           interpreter_rate=120),
    }
    yield ids
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


def _get_user(uid):
    return portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (uid,))


# ── 1. Migration backfill ─────────────────────────────────────────────────────

def test_backfill_splits_names(world):
    with open(MIG_PATH, encoding="utf-8") as f:
        mig = f.read()
    portal_db.execute(mig)  # idempotent; only touches rows with first_name IS NULL
    u = _get_user(world["int"])
    assert u["first_name"] == "Casey"
    assert u["middle_initial"] == "Q"
    assert u["last_name"] == "Terp"
    c = _get_user(world["client"])
    assert c["first_name"] == "Client"
    assert c["last_name"] == "Person"
    assert c["middle_initial"] is None


# ── 2. Save recomposes full_name; rate self-edit closed ───────────────────────

def _post_profile(c, extra=None, uid=None):
    data = {
        "csrf_token": _csrf(c), "action": "update",
        "first_name": "Dana", "middle_initial": "R", "last_name": "Signer",
        "phone": "619-555-0000", "zip": "92101",
        "address_street": "500 Harbor Dr", "address_city": "San Diego",
        "address_state": "CA",
    }
    if extra:
        data.update(extra)
    if uid:
        data["view_uid"] = str(uid)
    return c.post("/portal/profile", data=data)


def test_save_recomposes_full_name_and_ignores_rate(app, world):
    c = _client(app, INT_EMAIL)
    r = _post_profile(c, extra={"certification": "RID", "specialty": "K-12",
                                "interpreter_rate": "999"})
    assert r.status_code == 200
    u = _get_user(world["int"])
    assert u["full_name"] == "Dana R. Signer"
    assert u["first_name"] == "Dana" and u["last_name"] == "Signer"
    assert u["address_street"] == "500 Harbor Dr"
    assert u["address_state"] == "CA"
    assert float(u["interpreter_rate"]) == 80.0          # self-edit ignored
    assert u["address"] == "1 Legacy Way, Old Town"      # legacy column untouched


def test_admin_view_still_sets_rate(app, world):
    c = _client(app, ADMIN_EMAIL)
    r = _post_profile(c, extra={"certification": "CDI", "specialty": "Government",
                                "interpreter_rate": "95.50"},
                      uid=world["int"])
    assert r.status_code == 200
    u = _get_user(world["int"])
    assert float(u["interpreter_rate"]) == 95.50


def test_invalid_state_dropped(app, world):
    c = _client(app, INT_EMAIL)
    _post_profile(c, extra={"address_state": "ZZ"})
    u = _get_user(world["int"])
    assert u["address_state"] is None


# ── 3. Off-list dropdown values round-trip ────────────────────────────────────

def test_offlist_cert_specialty_render_selected(app, world):
    portal_db.execute(
        "UPDATE portal_users SET certification='BEI Master', specialty='Legal' WHERE id=%s",
        (world["int"],),
    )
    html = _client(app, INT_EMAIL).get("/portal/profile").data.decode()
    assert '<option value="BEI Master" selected>BEI Master</option>' in html
    assert '<option value="Legal" selected>Legal</option>' in html


# ── 4. Legacy address display ─────────────────────────────────────────────────

def test_legacy_address_shown_until_split_saved(app, world):
    html = _client(app, INT_EMAIL).get("/portal/profile").data.decode()
    assert "Address on file (legacy)" in html
    _post_profile(_client(app, INT_EMAIL))
    html = _client(app, INT_EMAIL).get("/portal/profile").data.decode()
    assert "Address on file (legacy)" not in html


# ── 5. Lists show "Last, First" ───────────────────────────────────────────────

def test_admin_lists_show_last_first(app, world):
    portal_db.execute(
        "UPDATE portal_users SET first_name='Casey', last_name='Terp' WHERE id=%s",
        (world["int"],),
    )
    html = _client(app, ADMIN_EMAIL).get("/portal/admin/interpreters").data.decode()
    assert "Terp, Casey" in html


# ── 6. rosesli interpreter form hides company/org and editable rate ───────────

def test_rosesli_interpreter_form_shape(app, world):
    html = _client(app, INT_EMAIL).get("/portal/profile").data.decode()
    assert 'name="company_name"' not in html
    assert 'name="interpreter_rate"' not in html
    assert "Current Base Rate" in html
    assert 'name="first_name"' in html
    assert "Exit without saving changes" in html
    assert "Upload Documents" in html
