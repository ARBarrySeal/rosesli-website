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
