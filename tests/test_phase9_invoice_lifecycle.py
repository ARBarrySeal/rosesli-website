"""Phase 1 (2026-07-22 batch) — individual job-linked invoices, auto-split
differentials, expenses, edit/lock, list split, bulk submit.
"""
import json
import secrets
from datetime import date, time

import pytest

import portal_db
from portal_auth import hash_password

COMPANY = "rosesli"
PW = "PytestPhase9_12345!"

ADMIN_EMAIL = "pytest-p9-admin@example.test"
INTERP_EMAIL = "pytest-p9-interp@example.test"
INTERP2_EMAIL = "pytest-p9-interp2@example.test"
EMAILS = [ADMIN_EMAIL, INTERP_EMAIL, INTERP2_EMAIL]


def _cleanup():
    uids = [r["id"] for r in portal_db.query_all(
        "SELECT id FROM portal_users WHERE email = ANY(%s) AND company = %s",
        (EMAILS, COMPANY))]
    if uids:
        portal_db.execute("DELETE FROM invoices WHERE user_id = ANY(%s)", (uids,))
        portal_db.execute("DELETE FROM jobs WHERE company = %s AND "
                           "(interpreter_1_id = ANY(%s) OR client_id = ANY(%s))",
                           (COMPANY, uids, uids))
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


def _mk_job(interpreter_id, event_date, start_time, end_time, status="confirmed"):
    row = portal_db.execute(
        "INSERT INTO jobs (company, event_date, start_time, end_time, duration, "
        "  status, interpreter_1_id, job_number, setting) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (COMPANY, event_date, start_time, end_time,
         _hours_between(start_time, end_time), status, interpreter_id,
         secrets.randbelow(900_000) + 100_000, "Medical"),
    )
    return row["id"]


def _hours_between(start_time, end_time):
    s = start_time.hour * 60 + start_time.minute
    e = end_time.hour * 60 + end_time.minute
    if e <= s:
        e += 24 * 60
    return round((e - s) / 60.0, 2)


@pytest.fixture
def world():
    _cleanup()
    ids = {
        "admin": _mk_user(ADMIN_EMAIL, "admin"),
        "interp": _mk_user(INTERP_EMAIL, "employee", interpreter_rate=50),
        "interp2": _mk_user(INTERP2_EMAIL, "employee", interpreter_rate=50),
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


# ── 1. Schema ──────────────────────────────────────────────────────────────

def test_invoices_table_has_job_id_and_expenses_columns():
    cols = {r["column_name"] for r in portal_db.query_all(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'invoices'")}
    assert "job_id" in cols
    assert "expenses" in cols


def test_job_id_unique_index_rejects_second_invoice_for_same_job(world):
    jid = _mk_job(world["interp"], date(2026, 8, 3), time(9, 0), time(11, 0))
    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, job_id) VALUES (%s, 10, 'unpaid', %s)",
        (world["interp"], jid))
    with pytest.raises(Exception):
        portal_db.execute(
            "INSERT INTO invoices (user_id, amount, status, job_id) VALUES (%s, 10, 'unpaid', %s)",
            (world["interp"], jid))


# ── 2. Time-band splitter ─────────────────────────────────────────────────

from portal_rates import compute_time_band_hours  # add to top imports


def test_split_pure_daytime_weekday():
    # Wed 2026-08-05, 9am-1pm: entirely inside 7a-5p on a weekday
    bands = compute_time_band_hours(date(2026, 8, 5), time(9, 0), time(13, 0))
    assert bands == {"day": 4.0}


def test_split_crosses_day_into_evening():
    # Wed 2026-08-05, 4pm-8pm: 1hr day (4-5p) + 3hr evening (5-8p)
    bands = compute_time_band_hours(date(2026, 8, 5), time(16, 0), time(20, 0))
    assert bands == {"day": 1.0, "weekday_evening": 3.0}


def test_split_overnight_crossing_midnight():
    # Fri 2026-08-07 11pm -> Sat 2026-08-08 2am: both portions are "overnight"
    # band by time-of-day, but the post-midnight portion is a WEEKEND day, so
    # it becomes weekend_overnight while the pre-midnight portion (still
    # Friday, a weekday) stays overnight.
    bands = compute_time_band_hours(date(2026, 8, 7), time(23, 0), time(2, 0))
    assert bands == {"overnight": 1.0, "weekend_overnight": 2.0}


def test_split_weekend_daytime():
    # Sat 2026-08-08, 10am-2pm
    bands = compute_time_band_hours(date(2026, 8, 8), time(10, 0), time(14, 0))
    assert bands == {"weekend_day": 4.0}


def test_split_applies_two_hour_minimum_proportionally():
    # Wed 2026-08-05, 9am-9:30am = 0.5h actual, entirely daytime.
    # Under the 2-hour minimum, billed hours scale to 2.0, still all "day"
    # since the shift never leaves that band.
    bands = compute_time_band_hours(date(2026, 8, 5), time(9, 0), time(9, 30))
    assert bands == {"day": 2.0}


def test_split_two_hour_minimum_scales_multi_band_proportionally():
    # Wed 2026-08-05, 4:45pm-5:15pm = 0.5h actual: 0.25h day + 0.25h evening.
    # Scaled to a 2h minimum (4x), each band scales to 1.0h.
    bands = compute_time_band_hours(date(2026, 8, 5), time(16, 45), time(17, 15))
    assert bands == {"day": 1.0, "weekday_evening": 1.0}


def test_split_rejects_zero_duration_shift():
    with pytest.raises(ValueError):
        compute_time_band_hours(date(2026, 8, 5), time(9, 0), time(9, 0))


# ── 3. Billable jobs excludes already-invoiced-by-job_id assignments ───────

def test_billable_jobs_excludes_job_linked_invoice(world):
    from portal_interpreter_invoices import billable_jobs_for_interpreter
    jid = _mk_job(world["interp"], date(2026, 8, 10), time(9, 0), time(11, 0))
    before = billable_jobs_for_interpreter(COMPANY, world["interp"])
    assert any(j["id"] == jid for j in before)

    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, job_id) VALUES (%s, 100, 'unpaid', %s)",
        (world["interp"], jid))

    after = billable_jobs_for_interpreter(COMPANY, world["interp"])
    assert not any(j["id"] == jid for j in after)


# ── 4. parse_expenses — informational only, never priced ───────────────────

from portal_admin import parse_expenses  # noqa: E402


def test_parse_expenses_collects_category_and_note_only():
    form = {
        "expense_category_0": "Parking", "expense_note_0": "Garage",
        "expense_category_1": "", "expense_note_1": "skip me",
        "expense_category_2": "Mileage", "expense_note_2": "",
    }
    lines = parse_expenses(form)
    assert lines == [
        {"category": "Parking", "note": "Garage"},
        {"category": "Mileage", "note": ""},
    ]


def test_parse_expenses_stops_at_first_gap():
    form = {"expense_category_0": "Parking", "expense_category_2": "Other"}
    assert len(parse_expenses(form)) == 1


# ── 5. /portal/api/time-bands ───────────────────────────────────────────────

def test_time_bands_endpoint_returns_split(app, world):
    c = _client(app, INTERP_EMAIL)
    r = c.get("/portal/api/time-bands?date=2026-08-05&start=16:00&end=20:00")
    assert r.status_code == 200
    assert r.get_json()["bands"] == {"day": 1.0, "weekday_evening": 3.0}


def test_time_bands_endpoint_returns_empty_on_bad_input(app, world):
    c = _client(app, INTERP_EMAIL)
    r = c.get("/portal/api/time-bands?date=&start=&end=")
    assert r.status_code == 200
    assert r.get_json()["bands"] == {}


# ── 6. Create redirects to detail; optional job_id link ────────────────────

def test_employee_create_invoice_redirects_to_detail(app, world):
    c = _client(app, INTERP_EMAIL)
    r = c.post("/portal/admin/invoices/create", data={
        "csrf_token": _csrf(c),
        "date_of_service": "2026-08-05", "start_time": "09:00", "end_time": "13:00",
        "base_rate": "50", "differential": "0", "amount": "200.00",
    }, follow_redirects=False)
    assert r.status_code == 302
    inv = portal_db.query_one(
        "SELECT * FROM invoices WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (world["interp"],))
    assert r.location.endswith(f"/portal/invoices/{inv['id']}")


def test_employee_create_invoice_links_billable_job(app, world):
    jid = _mk_job(world["interp"], date(2026, 8, 12), time(9, 0), time(11, 0))
    c = _client(app, INTERP_EMAIL)
    r = c.post("/portal/admin/invoices/create", data={
        "csrf_token": _csrf(c), "job_id": str(jid),
        "date_of_service": "2026-08-12", "start_time": "09:00", "end_time": "11:00",
        "base_rate": "50", "differential": "0", "amount": "100.00",
    }, follow_redirects=False)
    assert r.status_code == 302
    inv = portal_db.query_one(
        "SELECT * FROM invoices WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (world["interp"],))
    assert inv["job_id"] == jid


def test_employee_cannot_link_someone_elses_job(app, world):
    jid = _mk_job(world["interp2"], date(2026, 8, 13), time(9, 0), time(11, 0))
    c = _client(app, INTERP_EMAIL)
    r = c.post("/portal/admin/invoices/create", data={
        "csrf_token": _csrf(c), "job_id": str(jid),
        "amount": "100.00",
    }, follow_redirects=False)
    assert r.status_code == 302
    inv = portal_db.query_one(
        "SELECT * FROM invoices WHERE user_id = %s ORDER BY id DESC LIMIT 1",
        (world["interp"],))
    assert inv["job_id"] is None


# ── 7. Edit route permissions ───────────────────────────────────────────────

def _mk_invoice(uid, amount=100.0, submitted=False):
    row = portal_db.execute(
        "INSERT INTO invoices (user_id, amount, status, submitted) "
        "VALUES (%s, %s, 'unpaid', %s) RETURNING id",
        (uid, amount, submitted),
    )
    return row["id"]


def test_owner_can_edit_unsubmitted_invoice(app, world):
    inv_id = _mk_invoice(world["interp"], amount=100.0)
    c = _client(app, INTERP_EMAIL)
    r = c.post(f"/portal/invoices/{inv_id}/edit", data={
        "csrf_token": _csrf(c), "amount": "150.00", "description": "updated",
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    inv = portal_db.query_one("SELECT * FROM invoices WHERE id = %s", (inv_id,))
    assert float(inv["amount"]) == 150.0
    assert inv["description"] == "updated"


def test_owner_blocked_from_editing_after_submit(app, world):
    inv_id = _mk_invoice(world["interp"], amount=100.0, submitted=True)
    c = _client(app, INTERP_EMAIL)
    r = c.post(f"/portal/invoices/{inv_id}/edit", data={
        "csrf_token": _csrf(c), "amount": "999.00",
    }, follow_redirects=False)
    assert r.status_code == 302
    inv = portal_db.query_one("SELECT amount FROM invoices WHERE id = %s", (inv_id,))
    assert float(inv["amount"]) == 100.0


def test_other_employee_cannot_edit_invoice(app, world):
    inv_id = _mk_invoice(world["interp"], amount=100.0)
    c = _client(app, INTERP2_EMAIL)
    r = c.get(f"/portal/invoices/{inv_id}/edit")
    assert r.status_code == 403


def test_admin_can_edit_any_invoice(app, world):
    inv_id = _mk_invoice(world["interp"], amount=100.0, submitted=True)
    c = _client(app, ADMIN_EMAIL)
    r = c.post(f"/portal/invoices/{inv_id}/edit", data={
        "csrf_token": _csrf(c), "user_id": str(world["interp"]), "amount": "200.00",
    }, follow_redirects=False)
    assert r.status_code == 302, r.data
    inv = portal_db.query_one("SELECT amount FROM invoices WHERE id = %s", (inv_id,))
    assert float(inv["amount"]) == 200.0


# ── 8. Submit locks the invoice ─────────────────────────────────────────────

def test_submit_locks_invoice(app, world):
    inv_id = _mk_invoice(world["interp"], amount=100.0)
    c = _client(app, INTERP_EMAIL)
    r = c.post(f"/portal/invoices/{inv_id}/submit",
               data={"csrf_token": _csrf(c)}, follow_redirects=False)
    assert r.status_code == 302
    inv = portal_db.query_one("SELECT submitted FROM invoices WHERE id = %s", (inv_id,))
    assert inv["submitted"] is True


# ── 9. Batch submit scoped to caller and open invoices only ────────────────

def test_batch_submit_scoped_to_caller_and_open_only(app, world):
    mine_open_1 = _mk_invoice(world["interp"], amount=100.0)
    mine_open_2 = _mk_invoice(world["interp"], amount=50.0)
    mine_already = _mk_invoice(world["interp"], amount=75.0, submitted=True)
    others = _mk_invoice(world["interp2"], amount=60.0)

    c = _client(app, INTERP_EMAIL)
    r = c.post("/portal/invoices/submit-batch", data={
        "csrf_token": _csrf(c),
        "invoice_ids": [str(mine_open_1), str(mine_open_2), str(others)],
    }, follow_redirects=False)
    assert r.status_code == 302

    def submitted(iid):
        return portal_db.query_one(
            "SELECT submitted FROM invoices WHERE id = %s", (iid,))["submitted"]

    assert submitted(mine_open_1) is True
    assert submitted(mine_open_2) is True
    assert submitted(others) is False
    assert submitted(mine_already) is True  # was already submitted


# ── 10. List page splits open vs. submitted for employees ──────────────────

def test_invoices_list_splits_open_and_submitted(app, world):
    open_id = _mk_invoice(world["interp"], amount=10.0)
    sub_id = _mk_invoice(world["interp"], amount=20.0, submitted=True)
    c = _client(app, INTERP_EMAIL)
    html = c.get("/portal/invoices").get_data(as_text=True)
    assert "Not Yet Submitted" in html
    assert "Past (Submitted)" in html
    open_pos = html.index(f"#{open_id}")
    sub_pos = html.index(f"#{sub_id}")
    split_pos = html.index("Past (Submitted)")
    assert open_pos < split_pos < sub_pos
