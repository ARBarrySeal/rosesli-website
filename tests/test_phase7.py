"""Phase 7 — public request form fields + assignment documents (Rose SLI).

Covers:
  * /api/request persists the new optional fields (dress_code, POC, split
    event address) into the auto-created job, alongside the legacy requester_*
  * absent new fields are still accepted (public form stays backward compatible)
  * off-list dress_code / state values from the public form are dropped
  * job-scoped documents never appear in the user-documents page, and
    user documents never appear on the assignment detail's document list
  * only admins and interpreters staffed on the job can reach a job document
"""
import io
import os
import secrets
import datetime as dt

import pytest

import portal_db
from portal_auth import hash_password


COMPANY = os.environ.get("COMPANY_ID", "rosesli")
PW = "PytestPhase7_12345!"

ADMIN_EMAIL = "pytest-p7-admin@example.test"
INT_EMAIL = "pytest-p7-int@example.test"
OTHER_EMAIL = "pytest-p7-other@example.test"
EMAILS = [ADMIN_EMAIL, INT_EMAIL, OTHER_EMAIL]

REQ_EMAIL = "pytest-p7-requester@example.test"
JOB_MARKER = "pytest-p7-job-marker"
DOC_MARKER = "pytest-p7-doc.pdf"
EVENT_DATE = dt.date.today() + dt.timedelta(days=7)


# ── Setup / teardown ──────────────────────────────────────────────────────────

def _cleanup():
    portal_db.execute(
        "DELETE FROM portal_documents WHERE original_name = %s AND company = %s",
        (DOC_MARKER, COMPANY),
    )
    portal_db.execute(
        "DELETE FROM jobs WHERE company = %s AND "
        "(notes = %s OR requester_email = %s)",
        (COMPANY, JOB_MARKER, REQ_EMAIL),
    )
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


def _mk_job(**cols):
    cols.setdefault("company", COMPANY)
    cols.setdefault("status", "confirmed")
    cols.setdefault("notes", JOB_MARKER)
    keys = list(cols.keys())
    placeholders = ", ".join(["%s"] * len(keys))
    row = portal_db.execute(
        f"INSERT INTO jobs ({', '.join(keys)}) VALUES ({placeholders}) RETURNING id",
        tuple(cols[k] for k in keys),
    )
    return row["id"]


@pytest.fixture
def world():
    _cleanup()
    admin_id = _mk_user(ADMIN_EMAIL, "admin")
    int_id = _mk_user(INT_EMAIL, "employee", full_name="Casey Terp")
    other_id = _mk_user(OTHER_EMAIL, "employee", full_name="Outsider Terp")
    yield {"admin": admin_id, "int": int_id, "other": other_id}
    _cleanup()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _login(client, email):
    with client.session_transaction() as sess:
        sess["csrf_token"] = secrets.token_hex(32)
        csrf = sess["csrf_token"]
    r = client.post("/login", data={"email": email, "password": PW, "csrf_token": csrf})
    assert r.status_code == 200, r.data
    return client

def _csrf(client):
    with client.session_transaction() as sess:
        return sess["csrf_token"]

def _client(app, email):
    return _login(app.test_client(), email)


# ── 1. Public request → job fields ────────────────────────────────────────────

BASE_FORM = {
    "name": "Pytest Requester",
    "email": REQ_EMAIL,
    "phone": "(619) 555-0100",
    "org": "Pytest Org",
    "setting": "Medical",
    "client_count": "1",
    "format": "onsite",
    "zip": "92101",
    "date": EVENT_DATE.isoformat(),
    "start_time": "09:00",
    "end_time": "11:00",
    "details": "pytest-p7 details",
}

def _latest_request_job():
    return portal_db.query_one(
        "SELECT * FROM jobs WHERE company = %s AND requester_email = %s "
        "ORDER BY id DESC LIMIT 1",
        (COMPANY, REQ_EMAIL),
    )


def test_request_persists_new_fields(app, world):
    c = app.test_client()
    form = dict(BASE_FORM,
                dress_code="Business Casual",
                street="123 Main St", city="San Diego", state="CA",
                poc_name="Front Desk", poc_phone="(619) 555-0199",
                poc_email="poc@example.test")
    r = c.post("/api/request", data=form)
    assert r.status_code == 200, r.data
    job = _latest_request_job()
    assert job is not None
    assert job["requester_name"] == "Pytest Requester"
    assert job["requester_email"] == REQ_EMAIL
    assert job["dress_code"] == "Business Casual"
    assert job["event_street"] == "123 Main St"
    assert job["event_city"] == "San Diego"
    assert job["event_state"] == "CA"
    assert job["poc_name"] == "Front Desk"
    assert job["poc_phone"] == "(619) 555-0199"
    assert job["poc_email"] == "poc@example.test"


def test_request_without_new_fields_still_accepted(app, world):
    c = app.test_client()
    r = c.post("/api/request", data=BASE_FORM)
    assert r.status_code == 200, r.data
    job = _latest_request_job()
    assert job is not None
    assert job["dress_code"] is None
    assert job["event_street"] is None
    assert job["poc_name"] is None


def test_request_drops_offlist_dress_code_and_state(app, world):
    c = app.test_client()
    form = dict(BASE_FORM, dress_code="Clown suit", state="XX")
    r = c.post("/api/request", data=form)
    assert r.status_code == 200, r.data
    job = _latest_request_job()
    assert job["dress_code"] is None
    assert job["event_state"] is None


# ── 2. Job documents vs user documents isolation ─────────────────────────────

def _mk_doc(user_id, job_id=None):
    row = portal_db.execute(
        "INSERT INTO portal_documents "
        "(user_id, company, filename, original_name, mime_type, size_bytes, uploaded_by, job_id) "
        "VALUES (%s, %s, %s, %s, 'application/pdf', 123, %s, %s) RETURNING id",
        (user_id, COMPANY, "pytest-p7-" + secrets.token_hex(8) + ".pdf",
         DOC_MARKER, user_id, job_id),
    )
    return row["id"]


def test_job_docs_hidden_from_user_documents_page(app, world):
    job = _mk_job(interpreter_1_id=world["int"])
    _mk_doc(world["admin"], job_id=job)
    admin = _client(app, ADMIN_EMAIL)
    r = admin.get("/portal/documents")
    assert r.status_code == 200
    assert DOC_MARKER.encode() not in r.data


def test_user_docs_hidden_from_assignment_detail(app, world):
    job = _mk_job(interpreter_1_id=world["int"])
    _mk_doc(world["int"], job_id=None)          # plain user doc
    admin = _client(app, ADMIN_EMAIL)
    r = admin.get(f"/portal/assignments/{job}")
    assert r.status_code == 200
    assert DOC_MARKER.encode() not in r.data


def test_job_doc_listed_on_assignment_detail(app, world):
    job = _mk_job(interpreter_1_id=world["int"])
    _mk_doc(world["admin"], job_id=job)
    admin = _client(app, ADMIN_EMAIL)
    r = admin.get(f"/portal/assignments/{job}")
    assert r.status_code == 200
    assert DOC_MARKER.encode() in r.data


# ── 3. Access control on job documents ────────────────────────────────────────

def test_non_assigned_interpreter_denied_job_doc(app, world):
    job = _mk_job(interpreter_1_id=world["int"])
    doc = _mk_doc(world["admin"], job_id=job)
    outsider = _client(app, OTHER_EMAIL)
    r = outsider.get(f"/portal/assignments/{job}/documents/{doc}/download")
    assert r.status_code == 403


def test_assigned_interpreter_can_download_job_doc(app, world):
    # Upload through the real route so the bytes exist in local storage.
    job = _mk_job(interpreter_1_id=world["int"])
    admin = _client(app, ADMIN_EMAIL)
    pdf = io.BytesIO(b"%PDF-1.4 pytest-p7")
    r = admin.post(
        f"/portal/admin/assignments/{job}/documents",
        data={"files": (pdf, DOC_MARKER), "csrf_token": _csrf(admin)},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302, r.data
    doc = portal_db.query_one(
        "SELECT id FROM portal_documents WHERE job_id = %s AND original_name = %s",
        (job, DOC_MARKER),
    )
    assert doc is not None
    intp = _client(app, INT_EMAIL)
    r = intp.get(f"/portal/assignments/{job}/documents/{doc['id']}/download")
    assert r.status_code == 200
    assert b"pytest-p7" in r.data


def test_upload_rejects_disallowed_extension(app, world):
    job = _mk_job(interpreter_1_id=world["int"])
    admin = _client(app, ADMIN_EMAIL)
    evil = io.BytesIO(b"MZ fake exe")
    r = admin.post(
        f"/portal/admin/assignments/{job}/documents",
        data={"files": (evil, "malware.exe"), "csrf_token": _csrf(admin)},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert r.status_code == 302
    row = portal_db.query_one(
        "SELECT id FROM portal_documents WHERE job_id = %s", (job,),
    )
    assert row is None
