"""Scheduler — jobs/assignments CRUD (Rose SLI).

Admin creates and edits jobs and assigns interpreters; interpreters get a
read-only view of jobs assigned to them. Everything is scoped by company so
the DoD portal never sees Rose SLI assignments.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request

import portal_db
from portal_audit import log as audit_log
from portal_auth import admin_required, login_required

jobs_bp = Blueprint("jobs", __name__)

STATUSES = ("pending", "confirmed", "completed", "cancelled")
RATE_TYPES = ("hourly", "flat")


def _clients(company):
    return portal_db.query_all(
        "SELECT id, full_name, email FROM portal_users "
        "WHERE company = %s AND role = 'client' AND active = TRUE ORDER BY full_name",
        (company,),
    )


def _interpreters(company):
    return portal_db.query_all(
        "SELECT id, full_name, email FROM portal_users "
        "WHERE company = %s AND role = 'employee' AND active = TRUE ORDER BY full_name",
        (company,),
    )


def _name_for(user_id, company):
    if not user_id:
        return None
    row = portal_db.query_one(
        "SELECT full_name FROM portal_users WHERE id = %s AND company = %s",
        (int(user_id), company),
    )
    return row["full_name"] if row else None


def _int_or_none(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _float_or_none(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _date_or_none(raw):
    from datetime import date
    raw = (raw or "").strip()
    try:
        date.fromisoformat(raw)
        return raw
    except ValueError:
        return None


def _parse_job_form(form, company):
    """Map the admin job form to the jobs column set, snapshotting interpreter/client names."""
    client_id = _int_or_none(form.get("client_id"))
    interp1   = _int_or_none(form.get("interpreter_1_id"))
    interp2   = _int_or_none(form.get("interpreter_2_id"))
    status    = form.get("status") if form.get("status") in STATUSES else "pending"
    rate_type = form.get("rate_type") if form.get("rate_type") in RATE_TYPES else "hourly"

    client_name = (form.get("client_name") or "").strip() or _name_for(client_id, company)

    return {
        "status":             status,
        "client_id":          client_id,
        "client_name":        client_name,
        "client_address":     (form.get("client_address") or "").strip() or None,
        "event_address":      (form.get("event_address") or "").strip() or None,
        "event_zip":          (form.get("event_zip") or "").strip() or None,
        "setting":            (form.get("setting") or "").strip() or None,
        "service_format":     (form.get("service_format") or "").strip() or None,
        "dress_code":         (form.get("dress_code") or "").strip() or None,
        "deaf_clients":       (form.get("deaf_clients") or "").strip() or None,
        "poc_name":           (form.get("poc_name") or "").strip() or None,
        "poc_email":          (form.get("poc_email") or "").strip() or None,
        "poc_phone":          (form.get("poc_phone") or "").strip() or None,
        "event_date":         _date_or_none(form.get("event_date")),
        "start_time":         (form.get("start_time") or "").strip() or None,
        "end_time":           (form.get("end_time") or "").strip() or None,
        "duration":           (form.get("duration") or "").strip() or None,
        "num_interpreters":   _int_or_none(form.get("num_interpreters")),
        "interpreter_1_id":   interp1,
        "interpreter_2_id":   interp2,
        "interpreter_1_name": _name_for(interp1, company),
        "interpreter_2_name": _name_for(interp2, company),
        "client_rate":        _float_or_none(form.get("client_rate")),
        "rate_type":          rate_type,
        "notes":              (form.get("notes") or "").strip() or None,
    }


# ── Assignments list (admin + interpreters) ───────────────────────────────────

@jobs_bp.route("/portal/assignments")
@login_required
def assignments():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = int(g.user["sub"])

    if role == "admin":
        rows = portal_db.query_all(
            "SELECT * FROM jobs WHERE company = %s "
            "ORDER BY event_date IS NULL, event_date DESC, created_at DESC",
            (company,),
        )
    elif role == "employee":
        rows = portal_db.query_all(
            "SELECT * FROM jobs WHERE company = %s "
            "AND (interpreter_1_id = %s OR interpreter_2_id = %s) "
            "ORDER BY event_date IS NULL, event_date DESC, created_at DESC",
            (company, uid, uid),
        )
    else:
        abort(403)

    from datetime import date
    today = date.today()
    upcoming, past, undated = [], [], []
    for r in rows:
        d = r.get("event_date")
        if d is None:
            undated.append(r)
        elif d >= today:
            upcoming.append(r)
        else:
            past.append(r)
    # Upcoming reads best soonest-first; the SQL ordered DESC, so flip it.
    upcoming.reverse()

    return render_template(
        "portal_assignments.html",
        upcoming=upcoming, past=past, undated=undated, is_admin=(role == "admin"),
    )


@jobs_bp.route("/portal/assignments/<int:job_id>")
@login_required
def assignment_detail(job_id):
    company = g.user["company"]
    role    = g.user["role"]
    uid     = int(g.user["sub"])

    job = portal_db.query_one(
        "SELECT * FROM jobs WHERE id = %s AND company = %s", (job_id, company),
    )
    if not job:
        abort(404)
    if role == "employee" and uid not in (job.get("interpreter_1_id"), job.get("interpreter_2_id")):
        abort(403)
    if role == "client":
        abort(403)
    return render_template("portal_assignment_detail.html", job=job, is_admin=(role == "admin"))


# ── Admin create / edit ───────────────────────────────────────────────────────

@jobs_bp.route("/portal/admin/assignments/new", methods=["GET", "POST"])
@admin_required
def create_assignment():
    company = g.user["company"]

    if request.method == "GET":
        return render_template(
            "portal_assignment_edit.html",
            job=None, clients=_clients(company), interpreters=_interpreters(company),
            statuses=STATUSES, rate_types=RATE_TYPES,
        )

    data = _parse_job_form(request.form, company)
    cols = ["company", "source"] + list(data.keys())
    vals = [company, "manual"] + list(data.values())
    placeholders = ", ".join(["%s"] * len(cols))
    row = portal_db.execute(
        f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
        tuple(vals),
    )
    new_id = row["id"] if row else None
    audit_log("job_create", target=f"job:{new_id}", metadata={"status": data["status"]})
    flash("Assignment created.", "success")
    return redirect(f"/portal/assignments/{new_id}" if new_id else "/portal/assignments")


@jobs_bp.route("/portal/admin/assignments/<int:job_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_assignment(job_id):
    company = g.user["company"]
    job = portal_db.query_one(
        "SELECT * FROM jobs WHERE id = %s AND company = %s", (job_id, company),
    )
    if not job:
        abort(404)

    if request.method == "GET":
        return render_template(
            "portal_assignment_edit.html",
            job=job, clients=_clients(company), interpreters=_interpreters(company),
            statuses=STATUSES, rate_types=RATE_TYPES,
        )

    data = _parse_job_form(request.form, company)
    set_clause = ", ".join(f"{k} = %s" for k in data.keys())
    portal_db.execute(
        f"UPDATE jobs SET {set_clause} WHERE id = %s AND company = %s",
        tuple(data.values()) + (job_id, company),
    )
    audit_log("job_update", target=f"job:{job_id}", metadata={"status": data["status"]})
    flash("Assignment updated.", "success")
    return redirect(f"/portal/assignments/{job_id}")
