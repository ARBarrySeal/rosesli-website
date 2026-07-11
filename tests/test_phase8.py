"""Phase 8 — invoice differentials UI + admin Differentials settings.

Covers:
  * parse_extra_lines: shared parser for both invoice forms — edited hours,
    date only stored when it differs from the main service date, specialty
    label attached from the submitted code, date-then-specialty ordering
  * diff_options_json carries code/spec so the forms can tag lines; specialty
    rows append after the time-band rows (auto-select positions 0-5 stable)
  * client invoice create POST stores enriched line_items and totals them
  * admin Differentials settings page: list, effective-dated amount update,
    past service dates keep the old amount
  * dod parity: fallback dict unchanged, no Differentials nav or page seeding
"""
import json
import os
import secrets
from datetime import date, timedelta

import pytest

import portal_db
import portal_rates
from portal_auth import hash_password
from portal_client_invoices import diff_options_json, parse_extra_lines

COMPANY = "rosesli"
PW = "PytestPhase8_12345!"

ADMIN_EMAIL = "pytest-p8-admin@example.test"
CLIENT_EMAIL = "pytest-p8-client@example.test"
EMAILS = [ADMIN_EMAIL, CLIENT_EMAIL]


def _cleanup():
    uids = [r["id"] for r in portal_db.query_all(
        "SELECT id FROM portal_users WHERE email = ANY(%s) AND company = %s",
        (EMAILS, COMPANY))]
    if uids:
        portal_db.execute("DELETE FROM client_invoices WHERE client_id = ANY(%s)", (uids,))
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY))
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))
    portal_db.execute(
        "DELETE FROM differentials WHERE company = %s AND code = 'lmr' "
        "AND effective_date > '1970-01-01'", (COMPANY,))


def _mk_user(email, role, **cols):
    base = "INSERT INTO portal_users (email, password_hash, full_name, role, company, active"
    vals = [email, hash_password(PW), cols.pop("full_name", f"Pytest {role.title()}"),
            role, COMPANY, True]
    extra = ""
    for k, v in cols.items():
        extra += f", {k}"
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    row = portal_db.execute(
        f"{base}{extra}) VALUES ({placeholders}) RETURNING id", tuple(vals))
    return row["id"]


@pytest.fixture
def world():
    _cleanup()
    ids = {
        "admin": _mk_user(ADMIN_EMAIL, "admin"),
        "client": _mk_user(CLIENT_EMAIL, "client", interpreter_rate=120),
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


PREFIX = ("ci_extra_diff_", "ci_extra_dur_", "ci_extra_amt_",
          "ci_extra_date_", "ci_extra_code_")


# ── 1. Shared line parser ─────────────────────────────────────────────────────

def test_parse_extra_lines_edited_hours_and_amounts():
    form = {
        "ci_extra_diff_0": "5", "ci_extra_dur_0": "3.5", "ci_extra_amt_0": "437.50",
        "ci_extra_diff_1": "10", "ci_extra_dur_1": "2", "ci_extra_amt_1": "260.00",
    }
    lines = parse_extra_lines(form, PREFIX, COMPANY, main_date="2026-07-10")
    assert len(lines) == 2
    assert lines[0]["duration"] == 3.5 and lines[0]["amount"] == 437.50
    assert lines[1]["duration"] == 2.0 and lines[1]["amount"] == 260.00
    # single-date invoice: no date key on lines matching the main date
    assert all("date" not in l for l in lines)


def test_parse_extra_lines_date_then_specialty_grouping():
    form = {
        # later date first, to prove sorting
        "ci_extra_diff_0": "5", "ci_extra_dur_0": "2", "ci_extra_amt_0": "250",
        "ci_extra_date_0": "2026-07-12",
        # specialty line on the main date
        "ci_extra_diff_1": "0", "ci_extra_dur_1": "2", "ci_extra_amt_1": "0",
        "ci_extra_date_1": "2026-07-10", "ci_extra_code_1": "specialty_conference",
        # plain line on the main date (sorts before specialty on same date)
        "ci_extra_diff_2": "3", "ci_extra_dur_2": "2", "ci_extra_amt_2": "246",
        "ci_extra_date_2": "2026-07-10",
    }
    lines = parse_extra_lines(form, PREFIX, COMPANY, main_date="2026-07-10")
    assert len(lines) == 3
    # main-date lines first (no differing date), specialty after plain,
    # then the 07-12 line
    assert "date" not in lines[0] and "specialty" not in lines[0]
    assert lines[1].get("specialty") == "Conference (Specialty)"
    assert lines[2]["date"] == "2026-07-12"


def test_parse_extra_lines_ignores_unknown_codes():
    form = {"ci_extra_diff_0": "5", "ci_extra_dur_0": "2", "ci_extra_amt_0": "250",
            "ci_extra_code_0": "not_a_real_code"}
    lines = parse_extra_lines(form, PREFIX, COMPANY, main_date="2026-07-10")
    assert "specialty" not in lines[0]


# ── 2. Dropdown JSON shape ────────────────────────────────────────────────────

def test_diff_options_carry_code_and_specialty():
    opts = json.loads(diff_options_json(COMPANY))
    codes = [o["code"] for o in opts]
    assert codes[0] == "day"                      # auto-select position 0 stable
    spec = [o for o in opts if o["code"].startswith("specialty_")]
    assert spec, "specialty rows now offered in the dropdowns"
    # specialty rows all come after the time-band/priced rows
    first_spec = codes.index(spec[0]["code"])
    assert all(not c.startswith("specialty_") for c in codes[:first_spec])
    assert spec[0]["spec"]                        # label carried for tagging
    assert all(o["spec"] == "" for o in opts[:first_spec])


def test_admin_options_keep_xcl_sentinels():
    opts = json.loads(diff_options_json(COMPANY, label_style="admin"))
    assert opts[-1]["code"] == "xcl" and opts[-1]["value"] == "xcl"
    assert opts[-2]["code"] == "xcl48"


# ── 3. Client invoice create stores enriched lines ────────────────────────────

def test_client_invoice_create_with_lines(app, world):
    c = _client(app, ADMIN_EMAIL)
    r = c.post("/portal/admin/client-invoices/create", data={
        "csrf_token": _csrf(c),
        "client_id": world["client"],
        "date_of_service": "2026-07-10",
        "duration_hours": "2", "rate_per_hour": "120", "incidentals": "10",
        "ci_extra_diff_0": "5", "ci_extra_dur_0": "2", "ci_extra_amt_0": "250",
        "ci_extra_date_0": "2026-07-11",
        "ci_extra_diff_1": "0", "ci_extra_dur_1": "2", "ci_extra_amt_1": "0",
        "ci_extra_date_1": "2026-07-10", "ci_extra_code_1": "specialty_deafblind",
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    inv = portal_db.query_one(
        "SELECT * FROM client_invoices WHERE client_id = %s ORDER BY id DESC LIMIT 1",
        (world["client"],))
    lines = json.loads(inv["line_items"])
    assert len(lines) == 2
    assert lines[0].get("specialty") == "Deafblind" and "date" not in lines[0]
    assert lines[1]["date"] == "2026-07-11" and lines[1]["amount"] == 250.0
    # total = 2h × $120 + $10 incidentals + $250 + $0
    assert float(inv["total"]) == 500.0


def test_create_forms_render_with_codes(app, world):
    c = _client(app, ADMIN_EMAIL)
    ci = c.get("/portal/admin/client-invoices/create").get_data(as_text=True)
    ai = c.get("/portal/admin/invoices/create").get_data(as_text=True)
    assert '"code":' in ci and "specialty_" in ci
    assert '"code":' in ai and '"xcl"' in ai


# ── 4. Differentials settings page ────────────────────────────────────────────

def test_differentials_page_lists_rows(app, world):
    c = _client(app, ADMIN_EMAIL)
    html = c.get("/portal/admin/differentials").get_data(as_text=True)
    assert "Weekend/Holiday Overnight" in html or "weekend_overnight" in html
    assert "specialty" in html.lower()
    assert 'name="effective_date"' in html


def test_differentials_update_is_effective_dated(app, world):
    c = _client(app, ADMIN_EMAIL)
    eff = date(2026, 7, 1)
    r = c.post("/portal/admin/differentials", data={
        "csrf_token": _csrf(c), "code": "lmr", "amount": "12.5",
        "effective_date": eff.isoformat(),
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    after = {r2["code"]: float(r2["amount"])
             for r2 in portal_rates.differentials_for(COMPANY, eff)}
    before = {r2["code"]: float(r2["amount"])
              for r2 in portal_rates.differentials_for(COMPANY, eff - timedelta(days=1))}
    assert after["lmr"] == 12.5
    assert before["lmr"] == 10.0                  # past service dates unchanged


def test_differentials_rejects_unknown_code(app, world):
    c = _client(app, ADMIN_EMAIL)
    n_before = portal_db.query_one(
        "SELECT COUNT(*) AS n FROM differentials WHERE company = %s", (COMPANY,))["n"]
    r = c.post("/portal/admin/differentials", data={
        "csrf_token": _csrf(c), "code": "bogus", "amount": "99",
        "effective_date": "2026-07-01",
    }, follow_redirects=False)
    assert r.status_code == 302
    n_after = portal_db.query_one(
        "SELECT COUNT(*) AS n FROM differentials WHERE company = %s", (COMPANY,))["n"]
    assert n_after == n_before


def test_differentials_requires_admin(app, world):
    c = _client(app, CLIENT_EMAIL)
    assert c.get("/portal/admin/differentials").status_code in (302, 403)


# ── 5. dod parity ─────────────────────────────────────────────────────────────

def test_dod_options_unchanged():
    from portal_client_invoices import DIFFERENTIALS
    d = DIFFERENTIALS("dod")
    assert len(d) == 9 and not any(k.startswith("specialty_") for k in d)
    opts = json.loads(diff_options_json("dod"))
    assert not any(o["code"].startswith("specialty_") for o in opts)
