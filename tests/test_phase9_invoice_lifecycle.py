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
