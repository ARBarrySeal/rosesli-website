"""Phase 3 (2026-07-22 batch) — Interpreter email list.

`portal_offers.active_interpreter_emails(company)` pulls every active,
non-archived interpreter's email from their profile — the list Phase 6's
"Broadcast to all interpreters" blast will send to.
"""
import os
import secrets

import pytest

import portal_db
from portal_auth import hash_password
from portal_offers import active_interpreter_emails

COMPANY = os.environ.get("COMPANY_ID", "rosesli")
OTHER_COMPANY = "dod" if COMPANY == "rosesli" else "rosesli"
PW = "PytestPhase3_12345!"

ACTIVE_EMAIL = "pytest-p3-active@example.test"
INACTIVE_EMAIL = "pytest-p3-inactive@example.test"
ARCHIVED_EMAIL = "pytest-p3-archived@example.test"
CLIENT_EMAIL = "pytest-p3-client@example.test"
ADMIN_EMAIL = "pytest-p3-admin@example.test"
OTHER_CO_EMAIL = "pytest-p3-otherco@example.test"
EMAILS = [ACTIVE_EMAIL, INACTIVE_EMAIL, ARCHIVED_EMAIL, CLIENT_EMAIL, ADMIN_EMAIL, OTHER_CO_EMAIL]


def _cleanup():
    portal_db.execute("DELETE FROM portal_users WHERE email = ANY(%s)", (EMAILS,))


def _mk_user(email, role, company=COMPANY, **cols):
    base = "INSERT INTO portal_users (email, password_hash, full_name, role, company, active"
    vals = [email, hash_password(PW), cols.pop("full_name", f"Pytest {role.title()}"), role, company, cols.pop("active", True)]
    extra = ""
    for k, v in cols.items():
        extra += f", {k}"
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    row = portal_db.execute(f"{base}{extra}) VALUES ({placeholders}) RETURNING id", tuple(vals))
    return row["id"]


@pytest.fixture
def world():
    _cleanup()
    active_id = _mk_user(ACTIVE_EMAIL, "employee", full_name="Casey Active")
    _mk_user(INACTIVE_EMAIL, "employee", active=False)
    _mk_user(ARCHIVED_EMAIL, "employee", archived=True)
    _mk_user(CLIENT_EMAIL, "client")
    _mk_user(ADMIN_EMAIL, "admin")
    _mk_user(OTHER_CO_EMAIL, "employee", company=OTHER_COMPANY)
    yield {"active": active_id}
    _cleanup()


def test_includes_only_active_non_archived_interpreters(world):
    rows = active_interpreter_emails(COMPANY)
    emails = {r["email"] for r in rows}
    assert ACTIVE_EMAIL in emails
    assert INACTIVE_EMAIL not in emails
    assert ARCHIVED_EMAIL not in emails


def test_excludes_other_roles_and_other_company(world):
    rows = active_interpreter_emails(COMPANY)
    emails = {r["email"] for r in rows}
    assert CLIENT_EMAIL not in emails
    assert ADMIN_EMAIL not in emails
    assert OTHER_CO_EMAIL not in emails


def test_returns_id_and_full_name(world):
    rows = active_interpreter_emails(COMPANY)
    row = next(r for r in rows if r["email"] == ACTIVE_EMAIL)
    assert row["id"] == world["active"]
    assert row["full_name"] == "Casey Active"
