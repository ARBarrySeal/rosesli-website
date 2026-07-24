"""Scheduler — jobs/assignments CRUD (Rose SLI).

Admin creates and edits jobs and assigns interpreters; interpreters get a
read-only view of jobs assigned to them. Everything is scoped by company so
the DoD portal never sees Rose SLI assignments.
"""
from flask import (
    Blueprint, abort, flash, g, jsonify, redirect, render_template, request,
)

import portal_db
from portal_audit import log as audit_log
from portal_auth import admin_required, login_required

jobs_bp = Blueprint("jobs", __name__)

STATUSES      = ("pending", "confirmed", "completed", "cancelled")
RATE_TYPES    = ("hourly", "flat")
ASSIGNMENT_TYPES = (
    "Conference", "Educational", "Legal", "Medical",
    "Other", "Performance/Entertainment", "Private Event", "Work Related",
)
SERVICE_FORMATS = ("In-Person", "VRI", "VRS", "Phone/OPI")
DRESS_CODES = ("Business Professional", "Business Casual", "Casual", "Scrubs/Medical")


def _clients(company):
    return portal_db.query_all(
        "SELECT id, full_name, email, interpreter_rate, phone, point_of_contact "
        "FROM portal_users "
        "WHERE company = %s AND role = 'client' AND active = TRUE ORDER BY full_name",
        (company,),
    )


def _interpreters(company):
    """Return active interpreters sorted by last name, including rate + specialty.

    Each row gains display_name ("Last, First" when the Phase-3 name fields are
    set, falling back to full_name/email) for the assignment-form dropdowns."""
    rows = portal_db.query_all(
        "SELECT id, full_name, first_name, last_name, email, interpreter_rate, specialty "
        "FROM portal_users "
        "WHERE company = %s AND role = 'employee' AND active = TRUE",
        (company,),
    )
    for r in rows:
        if r.get("last_name"):
            r["display_name"] = f"{r['last_name']}, {r.get('first_name') or ''}".rstrip(", ")
        else:
            r["display_name"] = r["full_name"] or r["email"]
    def _last(r):
        if r.get("last_name"):
            return r["last_name"].lower()
        parts = (r["full_name"] or "").split()
        return parts[-1].lower() if parts else ""
    return sorted(rows, key=_last)


def _interpreters_for_form(company, event_date, staffed_ids=None):
    """Interpreters for the assignment-form dropdown: available on event_date
    (Phase 4.3), always including anyone already staffed on this job even if
    they'd otherwise be filtered out (a block added after staffing shouldn't
    make them vanish from their own assignment's dropdown)."""
    from portal_availability import available_interpreters
    rows = available_interpreters(company, event_date)
    if staffed_ids:
        have = {r["id"] for r in rows}
        missing = [i for i in staffed_ids if i not in have]
        if missing:
            all_rows = {r["id"]: r for r in _interpreters(company)}
            rows = rows + [all_rows[i] for i in missing if i in all_rows]
    return rows


def _specialties(raw):
    """Split a stored specialty string ('Legal, Medical') into a set of categories."""
    return {s.strip() for s in (raw or "").split(",") if s.strip()}


def interpreters_for_category(company, category):
    """Active interpreters, those whose specialty matches `category` first.

    Each row gains a `specialty_match` bool so callers/templates can flag matches.
    """
    rows = _interpreters(company)
    for r in rows:
        r["specialty_match"] = bool(category) and category in _specialties(r.get("specialty"))
    rows.sort(key=lambda r: (not r["specialty_match"],))
    return rows


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


def _parse_clock(raw):
    """Minutes since midnight for a clock string, or None. Accepts '9:00 AM',
    '9 AM', '2:30 PM', '14:30', '9'."""
    import re
    raw = (raw or "").strip().upper()
    if not raw:
        return None
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?$", raw)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = m.group(3)
    if minute > 59:
        return None
    if meridiem:
        if hour < 1 or hour > 12:
            return None
        if meridiem == "AM":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    elif hour > 23:
        return None
    return hour * 60 + minute


def _compute_duration(start, end):
    """Decimal-hours string ('2', '2.5', '0.75') between two clock strings, or
    None if either is unparseable or end is not after start (no overnight guess)."""
    s, e = _parse_clock(start), _parse_clock(end)
    if s is None or e is None or e <= s:
        return None
    hours = (e - s) / 60
    # Trim trailing zeros: 2.0 -> "2", 2.50 -> "2.5".
    return f"{hours:.2f}".rstrip("0").rstrip(".")


def _next_job_number(company):
    row = portal_db.query_one(
        "SELECT COALESCE(MAX(job_number), 0) AS n FROM jobs WHERE company = %s",
        (company,),
    )
    return (row["n"] or 0) + 1


def job_interpreter_rows(job_id):
    """Staffed interpreters for a job from the join table, slot order."""
    return portal_db.query_all(
        "SELECT interpreter_id, interpreter_name, slot FROM job_interpreters "
        "WHERE job_id = %s ORDER BY slot, id",
        (job_id,),
    )


def is_staffed_on(job_id, interpreter_id):
    """True if the interpreter holds a slot on this job (join table or legacy mirror)."""
    row = portal_db.query_one(
        "SELECT 1 FROM jobs WHERE id = %s "
        "AND (interpreter_1_id = %s OR interpreter_2_id = %s "
        "     OR EXISTS (SELECT 1 FROM job_interpreters ji "
        "                WHERE ji.job_id = jobs.id AND ji.interpreter_id = %s)) LIMIT 1",
        (job_id, interpreter_id, interpreter_id, interpreter_id),
    )
    return row is not None


def _mirror_slots(cur, job_id, company):
    """Write-through: keep jobs.interpreter_1_*/2_* synced to join-table slots
    1-2 so untouched read paths (dashboards, invoice pickers, dod) keep working."""
    cur.execute(
        "SELECT interpreter_id, interpreter_name FROM job_interpreters "
        "WHERE job_id = %s ORDER BY slot, id LIMIT 2",
        (job_id,),
    )
    rows = cur.fetchall()
    i1 = rows[0] if len(rows) > 0 else (None, None)
    i2 = rows[1] if len(rows) > 1 else (None, None)
    cur.execute(
        "UPDATE jobs SET interpreter_1_id = %s, interpreter_1_name = %s, "
        "interpreter_2_id = %s, interpreter_2_name = %s "
        "WHERE id = %s AND company = %s",
        (i1[0], i1[1], i2[0], i2[1], job_id, company),
    )


def set_job_interpreters(company, job_id, interpreter_ids):
    """Replace a job's staffing with the given ordered id list (edit form is
    authoritative), then mirror slots 1-2 into the legacy columns."""
    with portal_db.transaction() as cur:
        cur.execute("DELETE FROM job_interpreters WHERE job_id = %s", (job_id,))
        for slot, iid in enumerate(interpreter_ids, start=1):
            cur.execute(
                "INSERT INTO job_interpreters (job_id, interpreter_id, interpreter_name, slot) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (job_id, interpreter_id) DO NOTHING",
                (job_id, iid, _name_for(iid, company), slot),
            )
        _mirror_slots(cur, job_id, company)


def add_job_interpreter(company, job_id, interpreter_id):
    """Staff one interpreter at the next open slot (offer-confirm path — never
    overwrites an existing slot). Returns the number of filled slots."""
    with portal_db.transaction() as cur:
        cur.execute(
            "INSERT INTO job_interpreters (job_id, interpreter_id, interpreter_name, slot) "
            "SELECT %s, %s, %s, COALESCE(MAX(slot), 0) + 1 "
            "FROM job_interpreters WHERE job_id = %s "
            "ON CONFLICT (job_id, interpreter_id) DO NOTHING",
            (job_id, interpreter_id, _name_for(interpreter_id, company), job_id),
        )
        _mirror_slots(cur, job_id, company)
        cur.execute("SELECT COUNT(*) FROM job_interpreters WHERE job_id = %s", (job_id,))
        return cur.fetchone()[0]


def job_consumer_rows(job_id):
    """Deaf Consumer rows for a job, in entry order."""
    return portal_db.query_all(
        "SELECT id, name, email FROM job_consumers WHERE job_id = %s ORDER BY id",
        (job_id,),
    )


def set_job_consumers(job_id, consumers):
    """Replace a job's Deaf Consumer rows with (name, email) pairs from the
    edit form (the form is authoritative, same as interpreter staffing)."""
    with portal_db.transaction() as cur:
        cur.execute("DELETE FROM job_consumers WHERE job_id = %s", (job_id,))
        for name, email in consumers:
            cur.execute(
                "INSERT INTO job_consumers (job_id, name, email) VALUES (%s, %s, %s)",
                (job_id, name, email),
            )


def _parse_consumers(form):
    """Paired consumer_names/consumer_emails lists → [(name, email)], blanks dropped."""
    names  = form.getlist("consumer_names")
    emails = form.getlist("consumer_emails")
    out = []
    for i in range(max(len(names), len(emails))):
        name  = (names[i]  if i < len(names)  else "").strip() or None
        email = (emails[i] if i < len(emails) else "").strip() or None
        if name or email:
            out.append((name, email))
    return out


def _parse_job_form(form, company):
    """Map the admin job form to the jobs column set, snapshotting interpreter/client names.

    Returns (data, interpreter_ids): interpreter_ids is the full ordered
    staffing list for the join table; data mirrors slots 1-2 into the legacy
    columns (write-through compatibility)."""
    client_id = _int_or_none(form.get("client_id"))
    # New multi-row form posts repeated interpreter_ids; fall back to the
    # legacy two-select field names so old form posts keep working.
    ids, seen = [], set()
    for raw in form.getlist("interpreter_ids"):
        iid = _int_or_none(raw)
        if iid and iid not in seen:
            ids.append(iid)
            seen.add(iid)
    if not ids:
        for f in ("interpreter_1_id", "interpreter_2_id"):
            iid = _int_or_none(form.get(f))
            if iid and iid not in seen:
                ids.append(iid)
                seen.add(iid)
    interp1 = ids[0] if len(ids) > 0 else None
    interp2 = ids[1] if len(ids) > 1 else None
    status    = form.get("status") if form.get("status") in STATUSES else "pending"
    rate_type = form.get("rate_type") if form.get("rate_type") in RATE_TYPES else "hourly"
    atype     = form.get("assignment_type")
    if atype not in ASSIGNMENT_TYPES:
        atype = None

    client_name = (form.get("client_name") or "").strip() or _name_for(client_id, company)

    start_time = (form.get("start_time") or "").strip() or None
    end_time   = (form.get("end_time") or "").strip() or None
    duration   = (form.get("duration") or "").strip() or _compute_duration(start_time, end_time)

    # Blank Client rate resolves from the client's rate effective on the
    # SERVICE DATE (rate_history), falling back to the stored base rate.
    event_date = _date_or_none(form.get("event_date"))
    client_rate = _float_or_none(form.get("client_rate"))
    if client_rate is None and client_id:
        import portal_rates
        client_rate = portal_rates.rate_for(client_id, event_date)

    data = {
        "status":             status,
        "assignment_type":    atype,
        "client_id":          client_id,
        "client_name":        client_name,
        "event_address":      (form.get("event_address") or "").strip() or None,
        "event_street":       (form.get("event_street") or "").strip() or None,
        "event_city":         (form.get("event_city") or "").strip() or None,
        "event_state":        (form.get("event_state") or "").strip() or None,
        "room_number":        (form.get("room_number") or "").strip() or None,
        "event_zip":          (form.get("event_zip") or "").strip() or None,
        "service_format":     (form.get("service_format") or "").strip() or None,
        "dress_code":         (form.get("dress_code") or "").strip() or None,
        "poc_name":           (form.get("poc_name") or "").strip() or None,
        "poc_email":          (form.get("poc_email") or "").strip() or None,
        "poc_phone":          (form.get("poc_phone") or "").strip() or None,
        "event_date":         event_date,
        "start_time":         start_time,
        "end_time":           end_time,
        "duration":           duration,
        "num_interpreters":   _int_or_none(form.get("num_interpreters")),
        "num_required":       _int_or_none(form.get("num_required")),
        "interpreter_1_id":   interp1,
        "interpreter_2_id":   interp2,
        "interpreter_1_name": _name_for(interp1, company),
        "interpreter_2_name": _name_for(interp2, company),
        "client_rate":        client_rate,
        "rate_type":          rate_type,
    }
    # rosesli splits notes into admin-only vs interpreter-visible; the legacy
    # notes column is only written by the dod form (untouched for rosesli).
    if company == "rosesli":
        data["admin_notes"]       = (form.get("admin_notes") or "").strip() or None
        data["interpreter_notes"] = (form.get("interpreter_notes") or "").strip() or None
    else:
        data["notes"] = (form.get("notes") or "").strip() or None
    return data, ids


# ── Assignments list (admin + interpreters) ───────────────────────────────────

@jobs_bp.route("/portal/assignments")
@login_required
def assignments():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = int(g.user["sub"])

    # interpreter_names aggregates every staffed slot (join table), so jobs
    # with 3+ interpreters list everyone, not just the mirrored slots 1-2.
    _names_agg = (
        "(SELECT string_agg(ji.interpreter_name, ', ' ORDER BY ji.slot, ji.id) "
        " FROM job_interpreters ji WHERE ji.job_id = jobs.id) AS interpreter_names"
    )
    if role == "admin":
        rows = portal_db.query_all(
            f"SELECT jobs.*, {_names_agg} FROM jobs WHERE company = %s "
            "ORDER BY event_date IS NULL, event_date DESC, created_at DESC",
            (company,),
        )
    elif role == "employee":
        rows = portal_db.query_all(
            f"SELECT jobs.*, {_names_agg} FROM jobs WHERE company = %s "
            "AND (interpreter_1_id = %s OR interpreter_2_id = %s "
            "     OR EXISTS (SELECT 1 FROM job_interpreters ji "
            "                WHERE ji.job_id = jobs.id AND ji.interpreter_id = %s)) "
            "ORDER BY event_date IS NULL, event_date DESC, created_at DESC",
            (company, uid, uid, uid),
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
    if role == "employee" and not is_staffed_on(job_id, uid):
        # Phase 8 (2026-07-22 batch, 8.6): an interpreter who's accepted (but
        # not yet formally confirmed/staffed) can still view full details —
        # the acceptance-confirmation email links here for documents/notes.
        from portal_offers import has_open_or_accepted_offer
        if not has_open_or_accepted_offer(job_id, company, uid):
            abort(403)
    if role == "client":
        abort(403)

    offers = interpreters = None
    active_interpreter_count = None
    if role == "admin":
        from portal_offers import offers_for_job, active_interpreter_emails
        offers = offers_for_job(job_id, company)
        interpreters = interpreters_for_category(company, job.get("assignment_type"))
        active_interpreter_count = len(active_interpreter_emails(company))

    documents = portal_db.query_all(
        "SELECT id, original_name, size_bytes, created_at FROM portal_documents "
        "WHERE job_id = %s AND company = %s ORDER BY created_at DESC",
        (job_id, company),
    )

    return render_template(
        "portal_assignment_detail.html",
        job=job, is_admin=(role == "admin"),
        staffed=job_interpreter_rows(job_id),
        consumers=job_consumer_rows(job_id),
        documents=documents,
        offers=offers, interpreters=interpreters,
        active_interpreter_count=active_interpreter_count,
    )


# ── Requests (client view of own jobs; admin public-request inbox) ────────────

@jobs_bp.route("/portal/requests")
@login_required
def requests_list():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = int(g.user["sub"])

    if role == "client":
        rows = portal_db.query_all(
            "SELECT * FROM jobs WHERE company = %s AND client_id = %s "
            "ORDER BY event_date IS NULL, event_date DESC, created_at DESC",
            (company, uid),
        )
    elif role == "admin":
        rows = portal_db.query_all(
            "SELECT * FROM jobs WHERE company = %s AND source = 'public_request' "
            "ORDER BY created_at DESC",
            (company,),
        )
    else:
        abort(403)

    return render_template(
        "portal_requests.html", requests=rows, is_admin=(role == "admin"),
    )


# ── Admin create / edit / delete ──────────────────────────────────────────────

@jobs_bp.route("/portal/admin/assignments/new", methods=["GET", "POST"])
@admin_required
def create_assignment():
    company = g.user["company"]

    if request.method == "GET":
        return render_template(
            "portal_assignment_edit.html",
            job=None,
            staffed=[],
            consumers=[],
            clients=_clients(company),
            interpreters=_interpreters_for_form(company, None),
            declined_ids=[],
            statuses=STATUSES,
            rate_types=RATE_TYPES,
            assignment_types=ASSIGNMENT_TYPES,
            service_formats=SERVICE_FORMATS,
            dress_codes=DRESS_CODES,
        )

    data, interp_ids = _parse_job_form(request.form, company)
    jnum   = _next_job_number(company)
    cols   = ["company", "source", "job_number"] + list(data.keys())
    vals   = [company, "manual", jnum] + list(data.values())
    placeholders = ", ".join(["%s"] * len(cols))
    row = portal_db.execute(
        f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id",
        tuple(vals),
    )
    new_id = row["id"] if row else None
    if new_id:
        set_job_interpreters(company, new_id, interp_ids)
        set_job_consumers(new_id, _parse_consumers(request.form))
    audit_log("job_create", target=f"job:{new_id}", metadata={"status": data["status"]})
    if new_id and data["status"] == "confirmed":
        from portal_client_invoices import ensure_invoice_for_job
        ensure_invoice_for_job(company, new_id, int(g.user["sub"]))
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
        staffed = job_interpreter_rows(job_id)
        # Phase 8 (2026-07-22 batch, 8.8): grey out anyone who declined an
        # offer for THIS assignment specifically — doesn't affect their
        # availability on any other job.
        declined_ids = [
            r["interpreter_id"] for r in portal_db.query_all(
                "SELECT interpreter_id FROM job_offers "
                "WHERE job_id = %s AND company = %s AND status = 'declined'",
                (job_id, company),
            )
        ]
        return render_template(
            "portal_assignment_edit.html",
            job=job,
            staffed=staffed,
            consumers=job_consumer_rows(job_id),
            clients=_clients(company),
            interpreters=_interpreters_for_form(
                company, job.get("event_date"), [s["interpreter_id"] for s in staffed]),
            declined_ids=declined_ids,
            statuses=STATUSES,
            rate_types=RATE_TYPES,
            assignment_types=ASSIGNMENT_TYPES,
            service_formats=SERVICE_FORMATS,
            dress_codes=DRESS_CODES,
        )

    data, interp_ids = _parse_job_form(request.form, company)
    set_clause = ", ".join(f"{k} = %s" for k in data.keys())
    portal_db.execute(
        f"UPDATE jobs SET {set_clause} WHERE id = %s AND company = %s",
        tuple(data.values()) + (job_id, company),
    )
    set_job_interpreters(company, job_id, interp_ids)
    set_job_consumers(job_id, _parse_consumers(request.form))
    audit_log("job_update", target=f"job:{job_id}", metadata={"status": data["status"]})
    if data["status"] == "confirmed":
        from portal_client_invoices import ensure_invoice_for_job
        ensure_invoice_for_job(company, job_id, int(g.user["sub"]))
    flash("Assignment updated.", "success")
    return redirect(f"/portal/assignments/{job_id}")


@jobs_bp.route("/portal/admin/assignments/<int:job_id>/delete", methods=["POST"])
@admin_required
def delete_assignment(job_id):
    company = g.user["company"]
    job = portal_db.query_one(
        "SELECT id FROM jobs WHERE id = %s AND company = %s", (job_id, company),
    )
    if not job:
        abort(404)
    portal_db.execute("DELETE FROM jobs WHERE id = %s AND company = %s", (job_id, company))
    audit_log("job_delete", target=f"job:{job_id}")
    flash("Assignment deleted.", "success")
    return redirect("/portal/assignments")


# ── Calendar (admin only) ─────────────────────────────────────────────────────

@jobs_bp.route("/portal/calendar")
@login_required
def calendar_view():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = int(g.user["sub"])
    if role not in ("admin", "employee"):
        abort(403)
    import calendar as cal
    from datetime import date

    year  = request.args.get("year",  type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)
    if month < 1:
        month = 12; year -= 1
    elif month > 12:
        month = 1;  year += 1

    first_day   = date(year, month, 1)
    _, num_days = cal.monthrange(year, month)
    last_day    = date(year, month, num_days)

    if role == "admin":
        jobs = portal_db.query_all(
            "SELECT id, job_number, event_date, start_time, end_time, "
            "       organization, client_name, requester_name, status "
            "FROM jobs "
            "WHERE company = %s AND event_date >= %s AND event_date <= %s "
            "ORDER BY event_date, start_time",
            (company, first_day.isoformat(), last_day.isoformat()),
        )
    else:
        # Interpreters see only their own confirmed assignments.
        jobs = portal_db.query_all(
            "SELECT id, job_number, event_date, start_time, end_time, "
            "       organization, client_name, requester_name, status "
            "FROM jobs "
            "WHERE company = %s AND status = 'confirmed' "
            "AND (interpreter_1_id = %s OR interpreter_2_id = %s "
            "     OR EXISTS (SELECT 1 FROM job_interpreters ji "
            "                WHERE ji.job_id = jobs.id AND ji.interpreter_id = %s)) "
            "AND event_date >= %s AND event_date <= %s "
            "ORDER BY event_date, start_time",
            (company, uid, uid, uid, first_day.isoformat(), last_day.isoformat()),
        )

    # Build a dict: {day_number: [job, ...]}
    by_day: dict = {}
    for j in jobs:
        d = j["event_date"]
        if d:
            by_day.setdefault(d.day, []).append(j)

    # Build a 6-row × 7-col grid (same as calendar.monthcalendar but flat)
    weeks = cal.monthcalendar(year, month)

    prev_month = month - 1 if month > 1 else 12
    prev_year  = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year  = year if month < 12 else year + 1

    return render_template(
        "portal_calendar.html",
        year=year, month=month,
        month_name=first_day.strftime("%B %Y"),
        weeks=weeks,
        by_day=by_day,
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
        today=date.today(),
    )


# ── Interpreter JSON endpoint for the assignment-form dropdown ────────────────

@jobs_bp.route("/api/interpreters")
@admin_required
def interpreters_json():
    """Interpreters for the assignment form's dropdown. With ?date=, filters
    to those available that day (Phase 4.3) — the form refetches this on
    every Date change so the picker stays live as the coordinator edits."""
    company  = g.user["company"]
    date_raw = request.args.get("date") or None
    rows = _interpreters_for_form(company, date_raw)
    return jsonify([
        {
            "id":   r["id"],
            "name": r["display_name"],
            "rate": float(r["interpreter_rate"]) if r["interpreter_rate"] else None,
        }
        for r in rows
    ])


@jobs_bp.route("/api/clients")
@admin_required
def clients_json():
    """Client accounts + base rate, for auto-filling Client rate on the form."""
    company = g.user["company"]
    rows = _clients(company)
    return jsonify([
        {
            "id":   r["id"],
            "name": r["full_name"] or r["email"],
            "rate": float(r["interpreter_rate"]) if r["interpreter_rate"] else None,
        }
        for r in rows
    ])
