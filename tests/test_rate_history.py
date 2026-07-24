"""Phase 4 — effective-dated rate history + DB-backed differentials (Rose SLI).

Covers:
  * rate_for() boundary dates: before first row, on effective date, between
    rows, after latest; fallback to portal_users.interpreter_rate
  * migration 015 backfill: one 1970-01-01 row per rated user (re-runnable)
  * set_rate() recalc rule (locked decision 7): non-completed jobs and unpaid,
    unsubmitted invoices with service date >= effective date get the new rate;
    paid invoices and earlier service dates are frozen
  * legacy interpreter_rate column stays synced to the currently-effective rate
  * differentials come from the DB (specialty placeholders excluded); dod falls
    back to the hardcoded dict
  * admin profile view renders the Rate History section
"""
import os
import secrets
from datetime import date, timedelta

import pytest

import portal_db
import portal_rates
from portal_auth import hash_password
from portal_client_invoices import DIFFERENTIALS, diff_options_json


COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase4_12345!"

ADMIN_EMAIL = "pytest-p4-admin@example.test"
INT_EMAIL = "pytest-p4-int@example.test"
CLIENT_EMAIL = "pytest-p4-client@example.test"
EMAILS = [ADMIN_EMAIL, INT_EMAIL, CLIENT_EMAIL]

MIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "migrations", "015_rate_history_differentials.sql")


def _cleanup():
    uids = [r["id"] for r in portal_db.query_all(
        "SELECT id FROM portal_users WHERE email = ANY(%s) AND company = %s",
        (EMAILS, COMPANY))]
    if uids:
        portal_db.execute("DELETE FROM invoices WHERE user_id = ANY(%s)", (uids,))
        portal_db.execute("DELETE FROM client_invoices WHERE client_id = ANY(%s)", (uids,))
        portal_db.execute("DELETE FROM jobs WHERE client_id = ANY(%s)", (uids,))
        portal_db.execute("DELETE FROM rate_history WHERE user_id = ANY(%s)", (uids,))
    portal_db.execute(
        "DELETE FROM portal_users WHERE email = ANY(%s) AND company = %s", (EMAILS, COMPANY))
    portal_db.execute("DELETE FROM portal_audit WHERE email = ANY(%s)", (EMAILS,))


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
        "int": _mk_user(INT_EMAIL, "employee", interpreter_rate=80),
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


# ── 1. Resolver boundaries ────────────────────────────────────────────────────

def test_rate_for_boundaries(world):
    uid = world["int"]
    portal_db.execute(
        "INSERT INTO rate_history (company, user_id, rate, effective_date) VALUES "
        "(%s, %s, 80, '2026-01-01'), (%s, %s, 90, '2026-07-01')",
        (COMPANY, uid, COMPANY, uid))
    assert portal_rates.rate_for(uid, date(2025, 12, 31)) == 80.0  # falls back to column
    assert portal_rates.rate_for(uid, date(2026, 1, 1)) == 80.0    # on first effective
    assert portal_rates.rate_for(uid, date(2026, 6, 30)) == 80.0   # between rows
    assert portal_rates.rate_for(uid, date(2026, 7, 1)) == 90.0    # on new effective
    assert portal_rates.rate_for(uid, date(2027, 1, 1)) == 90.0    # after latest
    assert portal_rates.rate_for(uid) == 80.0                      # no date → legacy column
    assert portal_rates.rate_for(None, date(2026, 7, 1)) is None


def test_backfill_reruns_clean(world):
    with open(MIG_PATH, encoding="utf-8") as f:
        sql = f.read()
    with portal_db.transaction() as cur:
        cur.execute(sql)
    rows = portal_db.query_all(
        "SELECT user_id, rate, effective_date FROM rate_history "
        "WHERE user_id IN (%s, %s)", (world["int"], world["client"]))
    assert {(r["user_id"], float(r["rate"]), str(r["effective_date"])) for r in rows} == {
        (world["int"], 80.0, "1970-01-01"),
        (world["client"], 120.0, "1970-01-01"),
    }
    # Second run must not duplicate.
    with portal_db.transaction() as cur:
        cur.execute(sql)
    n = portal_db.query_one(
        "SELECT COUNT(*) AS n FROM rate_history WHERE user_id = %s", (world["int"],))
    assert n["n"] == 1


# ── 2. set_rate recalc (money rule — decision 7) ─────────────────────────────

def _mk_job(client_id, event_date, status, rate=120):
    row = portal_db.execute(
        "INSERT INTO jobs (company, status, client_id, client_name, event_date, client_rate) "
        "VALUES (%s, %s, %s, 'Pytest Client', %s, %s) RETURNING id",
        (COMPANY, status, client_id, event_date, rate))
    return row["id"]


def _mk_client_invoice(client_id, dos, status, rate=120, hours=2):
    row = portal_db.execute(
        "INSERT INTO client_invoices (company, client_id, client_name, date_of_service, "
        "  duration_hours, rate_per_hour, total, status) "
        "VALUES (%s, %s, 'Pytest Client', %s, %s, %s, %s, %s) RETURNING id",
        (COMPANY, client_id, dos, hours, rate, hours * rate, status))
    return row["id"]


def test_client_rate_change_recalcs_future_unpaid_only(world):
    # Dates anchored to date.today() (not hardcoded absolutes) so this test's
    # "still in the future" assumption never silently expires as time passes.
    today = date.today()
    effective = today + timedelta(days=10)
    far_future = today + timedelta(days=25)
    past = today - timedelta(days=25)

    cid = world["client"]
    j_future = _mk_job(cid, far_future.isoformat(), "confirmed")
    j_past = _mk_job(cid, past.isoformat(), "confirmed")
    j_done = _mk_job(cid, far_future.isoformat(), "completed")
    i_unpaid = _mk_client_invoice(cid, far_future.isoformat(), "unpaid")
    i_paid = _mk_client_invoice(cid, far_future.isoformat(), "paid")
    i_old = _mk_client_invoice(cid, past.isoformat(), "unpaid")

    summary = portal_rates.set_rate(COMPANY, cid, 150, effective,
                                    created_by=world["admin"])
    assert summary["jobs"] == 1
    assert summary["client_invoices"] == 1

    get = lambda t, i: portal_db.query_one(f"SELECT * FROM {t} WHERE id = %s", (i,))  # noqa: E731
    assert float(get("jobs", j_future)["client_rate"]) == 150.0
    assert float(get("jobs", j_past)["client_rate"]) == 120.0     # earlier service date frozen
    assert float(get("jobs", j_done)["client_rate"]) == 120.0     # completed frozen
    ui = get("client_invoices", i_unpaid)
    assert float(ui["rate_per_hour"]) == 150.0
    assert float(ui["total"]) == 300.0                            # 2h × 150 recomputed
    assert float(get("client_invoices", i_paid)["rate_per_hour"]) == 120.0   # paid frozen
    assert float(get("client_invoices", i_old)["rate_per_hour"]) == 120.0    # old DOS frozen
    # effective_date (today+10) is still in the future relative to today, so
    # the legacy column keeps the currently-effective 120 while future work
    # resolves 150.
    u = portal_db.query_one("SELECT interpreter_rate FROM portal_users WHERE id = %s", (cid,))
    assert float(u["interpreter_rate"]) == 120.0
    assert portal_rates.rate_for(cid, far_future) == 150.0


def test_interpreter_rate_change_recalcs_unsubmitted_unpaid(world):
    uid = world["int"]
    mk = lambda dos, status, submitted: portal_db.execute(  # noqa: E731
        "INSERT INTO invoices (user_id, amount, status, submitted, date_of_service, "
        "  duration_hours, base_rate, differential, rate_applied) "
        "VALUES (%s, 170, %s, %s, %s, 2, 80, '5', 85) RETURNING id",
        (uid, status, submitted, dos))["id"]
    i_open = mk("2026-08-01", "unpaid", False)
    i_submitted = mk("2026-08-01", "unpaid", True)
    i_paid = mk("2026-08-01", "paid", False)
    i_old = mk("2026-06-01", "unpaid", False)

    summary = portal_rates.set_rate(COMPANY, uid, 100, date(2026, 7, 15),
                                    created_by=world["admin"])
    assert summary["interpreter_invoices"] == 1

    get = lambda i: portal_db.query_one("SELECT * FROM invoices WHERE id = %s", (i,))  # noqa: E731
    o = get(i_open)
    assert float(o["base_rate"]) == 100.0
    assert float(o["rate_applied"]) == 105.0        # base 100 + $5 differential
    assert float(o["amount"]) == 210.0              # 2h × 105
    assert float(get(i_submitted)["base_rate"]) == 80.0
    assert float(get(i_paid)["base_rate"]) == 80.0
    assert float(get(i_old)["base_rate"]) == 80.0


def test_future_effective_date_keeps_current_column(world):
    uid = world["int"]
    portal_rates.set_rate(COMPANY, uid, 200, date(2099, 1, 1), created_by=world["admin"])
    u = portal_db.query_one("SELECT interpreter_rate FROM portal_users WHERE id = %s", (uid,))
    assert float(u["interpreter_rate"]) == 80.0     # not effective yet
    assert portal_rates.rate_for(uid, date(2099, 1, 2)) == 200.0


# ── 3. Differentials from the DB ─────────────────────────────────────────────

def test_differentials_db_backed():
    d = DIFFERENTIALS("rosesli")
    assert d["weekend_overnight"][1] == 15.0
    assert d["lmr"][1] == 10.0
    assert not any(code.startswith("specialty_") for code in d)


def test_diff_options_json_shapes():
    import json
    client_opts = json.loads(diff_options_json("rosesli"))
    admin_opts = json.loads(diff_options_json("rosesli", label_style="admin"))
    assert client_opts[0]["value"] == 0          # daytime weekday first (auto-select)
    assert "(+$" in client_opts[1]["label"]
    assert "(BR+$" in admin_opts[1]["label"]
    assert admin_opts[-1]["value"] == "xcl"      # XCL sentinel appended for admin
    assert len(admin_opts) == len(client_opts) + 2


def test_dod_differentials_fall_back():
    d = DIFFERENTIALS("dod")
    assert d["day"][1] == 0                      # hardcoded fallback dict
    assert len(d) == 9


# ── 4. Admin UI ───────────────────────────────────────────────────────────────

def test_admin_profile_shows_rate_history(app, world):
    portal_db.execute(
        "INSERT INTO rate_history (company, user_id, rate, effective_date) "
        "VALUES (%s, %s, 80, '2026-01-01')", (COMPANY, world["int"]))
    c = _client(app, ADMIN_EMAIL)
    html = c.get(f"/portal/profile?uid={world['int']}").get_data(as_text=True)
    assert "Rate History" in html
    assert 'name="new_rate"' in html
    assert 'name="interpreter_rate"' not in html  # direct edit gone (rosesli)


def test_own_profile_has_no_rate_history(app, world):
    c = _client(app, INT_EMAIL)
    html = c.get("/portal/profile").get_data(as_text=True)
    assert 'name="new_rate"' not in html
