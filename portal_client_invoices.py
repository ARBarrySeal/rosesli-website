"""Client invoice CRUD — Rose SLI portal (admin only create/edit)."""
import json

from flask import Blueprint, abort, flash, g, redirect, render_template, request

import portal_db
import portal_rates
from portal_audit import log as audit_log
from portal_auth import admin_required, login_required

client_inv_bp = Blueprint("client_invoices", __name__)

# Fallback only — since mig 015 differentials live in the DB (seeded from this
# table) and are read via portal_rates.differentials_for().
_DIFFERENTIALS_FALLBACK = {
    "day":                ("Daytime / Weekdays 7a–5p",   0),
    "weekend_day":        ("Weekend Day (Sat/Sun) 7a–5p",  5),
    "weekday_evening":    ("Weekday Evening 5p–10p",        5),
    "weekend_evening":    ("Weekend Evening 5p–10p",       10),
    "overnight":          ("Overnight 10p–7a",             10),
    "weekend_overnight":  ("Weekend Overnight 10p–7a",     15),
    "conference":         ("Conference",                   15),
    "holiday":            ("Holiday",                      12),
    "lmr":                ("LMR (<24 hr notice)",          10),
}


def DIFFERENTIALS(company="rosesli", service_date=None, include_specialty=False):
    """DB-backed replacement for the old hardcoded dict: {code: (label, amount)}."""
    try:
        rows = portal_rates.differentials_for(company, service_date,
                                              include_specialty=include_specialty)
        if rows:
            return {r["code"]: (r["label"], float(r["amount"])) for r in rows}
    except Exception:
        pass
    return dict(_DIFFERENTIALS_FALLBACK)


def diff_options_json(company="rosesli", service_date=None, label_style="client"):
    """JSON for the invoice-form dropdowns — replaces the duplicated
    hand-maintained DIFF_OPTIONS arrays in both create templates. Order
    matters: the forms' auto-select maps day/evening/overnight bands to
    positions 0–5, which the DB rows' sort_order preserves (specialty rows sort
    after 100 so appending them can't shift those indices). Each entry carries
    the row's code + specialty label so the form can tag submitted lines."""
    opts = []
    for code, (label, amt) in DIFFERENTIALS(company, service_date,
                                            include_specialty=True).items():
        amt = float(amt)
        is_spec = code.startswith("specialty_")
        if label_style == "admin":
            suffix = f" (BR+${amt:g}/hr)" if amt else " (BR)"
        else:
            suffix = f" (+${amt:g}/hr)" if amt else ""
        opts.append({"label": f"{label}{suffix}", "value": amt, "code": code,
                     "spec": label if is_spec else ""})
    if label_style == "admin":
        # Cancellation entries are billing statuses, not priced differentials —
        # 'xcl' is a string sentinel the form JS special-cases, so these never
        # live in the differentials table.
        opts.append({"label": "XCL<48 — Cancellation <48hr (BR)", "value": 0,
                     "code": "xcl48", "spec": ""})
        opts.append({"label": "XCL — No charge", "value": "xcl",
                     "code": "xcl", "spec": ""})
    return json.dumps(opts)


def parse_extra_lines(form, prefix, company, main_date=None):
    """Collect the dynamic differential lines from an invoice-create post.

    `prefix` maps the per-line field names, e.g.
      ("ci_extra_diff_", "ci_extra_dur_", "ci_extra_amt_", "ci_extra_date_", "ci_extra_code_")

    Line shape stays backward compatible ({differential, duration, amount});
    Phase 8 adds `date` (only when it differs from the invoice's main service
    date — so single-date invoices keep the old compact shape) and `specialty`
    (label, only for specialty_* differential rows). Lines come back grouped
    by date, then specialty."""
    diff_f, dur_f, amt_f, date_f, code_f = prefix
    try:
        spec_labels = {
            r["code"]: r["label"]
            for r in portal_rates.differentials_for(company, main_date,
                                                    include_specialty=True)
            if r["code"].startswith("specialty_")
        }
    except Exception:
        spec_labels = {}
    lines = []
    idx = 0
    while True:
        ed = form.get(f"{diff_f}{idx}")
        if ed is None:
            break
        try:
            ea = float(form.get(f"{amt_f}{idx}") or 0)
        except ValueError:
            ea = 0.0
        try:
            edur = float(form.get(f"{dur_f}{idx}") or 0)
        except ValueError:
            edur = 0.0
        line = {"differential": ed, "duration": edur, "amount": ea}
        edate = _date_or_none(form.get(f"{date_f}{idx}"))
        if edate and edate != main_date:
            line["date"] = edate
        code = (form.get(f"{code_f}{idx}") or "").strip()
        if code in spec_labels:
            line["specialty"] = spec_labels[code]
        lines.append(line)
        idx += 1
    lines.sort(key=lambda l: (l.get("date") or main_date or "",
                              l.get("specialty") or ""))
    return lines

HOLIDAYS = (
    "New Year's Day", "MLK Day", "Presidents' Day", "Cesar Chavez Day",
    "Memorial Day", "Juneteenth", "Independence Day", "Labor Day",
    "Veterans Day", "Thanksgiving Day", "Christmas Day",
)


def _clients(company):
    return portal_db.query_all(
        "SELECT id, full_name, email FROM portal_users "
        "WHERE company = %s AND role = 'client' AND active = TRUE "
        "ORDER BY full_name",
        (company,),
    )


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


def ensure_invoice_for_job(company, job_id, created_by=None):
    """Auto-create an unpaid client invoice for a *confirmed* billable job.

    Best-effort and idempotent: fires only when the job is confirmed and has
    both a client account and a client rate, and dedups on job_id so re-saving
    a confirmed assignment never spawns duplicates. Any failure is swallowed so
    auto-billing can never break the assignment save itself. Returns the invoice
    id (existing or new), or None when nothing was created.
    """
    try:
        job = portal_db.query_one(
            "SELECT * FROM jobs WHERE id = %s AND company = %s", (job_id, company),
        )
        if not job or job.get("status") != "confirmed":
            return None
        if not job.get("client_id"):
            return None
        # Snapshot rate from the job; when absent, resolve the client's rate
        # effective on the SERVICE DATE (rate_history).
        rate = job.get("client_rate")
        if rate is None:
            rate = portal_rates.rate_for(job["client_id"], job.get("event_date"))
        if rate is None:
            return None

        existing = portal_db.query_one(
            "SELECT id FROM client_invoices WHERE job_id = %s AND company = %s",
            (job_id, company),
        )
        if existing:
            return existing["id"]

        rate = float(rate)
        dur_h = _float_or_none(str(job.get("duration") or ""))
        total = round(dur_h * rate, 2) if dur_h else None

        row = portal_db.execute(
            "INSERT INTO client_invoices "
            "(company, client_id, client_name, poc_email, poc_phone, date_of_service, "
            "start_time, end_time, duration_hours, rate_per_hour, incidentals, total, "
            "notes, job_id, created_by, submitted) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE) RETURNING id",
            (company, job["client_id"], job.get("client_name"), job.get("poc_email"),
             job.get("poc_phone"), job.get("event_date"), job.get("start_time"),
             job.get("end_time"), dur_h, rate, 0.0, total, None, job_id, created_by),
        )
        return row["id"] if row else None
    except Exception:
        return None


def _prefill_from_job(company, raw_job_id):
    """Build a prefill dict for the create form from a request/assignment job."""
    try:
        job_id = int(raw_job_id)
    except (TypeError, ValueError):
        return {}
    job = portal_db.query_one(
        "SELECT * FROM jobs WHERE id = %s AND company = %s", (job_id, company),
    )
    if not job:
        return {}
    event_date = job.get("event_date")
    # Only assignment types with one obvious specialty row auto-offer a line;
    # "Educational" is ambiguous (K-12 vs Higher Ed) so it stays manual.
    specialty_code = {
        "Conference":                "specialty_conference",
        "Performance/Entertainment": "specialty_performance",
    }.get(job.get("assignment_type") or "", "")
    return {
        "job_id":          job["id"],
        "specialty_code":  specialty_code,
        "client_id":       job.get("client_id"),
        "client_name":     job.get("client_name") or job.get("requester_name"),
        "poc_email":       job.get("poc_email") or job.get("requester_email"),
        "poc_phone":       job.get("poc_phone") or job.get("requester_phone"),
        "date_of_service": event_date.isoformat() if event_date else "",
        "duration_hours":  _float_or_none(str(job.get("duration") or "")) or "",
        "rate_per_hour":   job.get("client_rate") or "",
    }


@client_inv_bp.route("/portal/client-invoices")
@login_required
def client_invoices_list():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        rows = portal_db.query_all(
            "SELECT ci.*, u.full_name AS user_full_name, j.job_number "
            "FROM client_invoices ci "
            "LEFT JOIN portal_users u ON u.id = ci.client_id "
            "LEFT JOIN jobs j ON j.id = ci.job_id "
            "WHERE ci.company = %s ORDER BY ci.created_at DESC",
            (company,),
        )
    elif role == "client":
        rows = portal_db.query_all(
            "SELECT ci.*, j.job_number FROM client_invoices ci "
            "LEFT JOIN jobs j ON j.id = ci.job_id "
            "WHERE ci.company = %s AND ci.client_id = %s AND ci.submitted = TRUE "
            "ORDER BY ci.created_at DESC",
            (company, uid),
        )
    else:
        rows = []

    return render_template("portal_client_invoices.html", invoices=rows)


@client_inv_bp.route("/portal/client-invoices/<int:inv_id>")
@login_required
def client_invoice_detail(inv_id):
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        inv = portal_db.query_one(
            "SELECT ci.*, u.full_name AS user_full_name, j.job_number "
            "FROM client_invoices ci "
            "LEFT JOIN portal_users u ON u.id = ci.client_id "
            "LEFT JOIN jobs j ON j.id = ci.job_id "
            "WHERE ci.id = %s AND ci.company = %s",
            (inv_id, company),
        )
    elif role == "client":
        inv = portal_db.query_one(
            "SELECT ci.*, j.job_number FROM client_invoices ci "
            "LEFT JOIN jobs j ON j.id = ci.job_id "
            "WHERE ci.id = %s AND ci.company = %s AND ci.client_id = %s AND ci.submitted = TRUE",
            (inv_id, company, uid),
        )
    else:
        abort(403)

    if not inv:
        abort(404)
    return render_template("portal_client_invoice.html", inv=inv)


@client_inv_bp.route("/portal/admin/client-invoices/create", methods=["GET", "POST"])
@admin_required
def create_client_invoice():
    company = g.user["company"]
    clients = _clients(company)

    if request.method == "GET":
        prefill = _prefill_from_job(company, request.args.get("job"))
        return render_template("portal_client_invoice_create.html",
                               clients=clients, prefill=prefill,
                               diff_options_json=diff_options_json(company))

    client_id = request.form.get("client_id") or None
    if client_id:
        target = portal_db.query_one(
            "SELECT id, full_name FROM portal_users WHERE id = %s AND company = %s",
            (int(client_id), company),
        )
        if not target:
            return render_template("portal_client_invoice_create.html",
                                   clients=clients, error="Invalid client.",
                                   diff_options_json=diff_options_json(company))
        client_name = target["full_name"]
        client_id   = target["id"]
    else:
        client_name = (request.form.get("client_name") or "").strip()
        client_id   = None

    poc_email       = (request.form.get("poc_email") or "").strip() or None
    poc_phone       = (request.form.get("poc_phone") or "").strip() or None
    date_of_service = _date_or_none(request.form.get("date_of_service"))
    start_time      = (request.form.get("start_time") or "").strip() or None
    end_time        = (request.form.get("end_time") or "").strip() or None
    duration_hours  = _float_or_none(request.form.get("duration_hours"))
    incidentals     = _float_or_none(request.form.get("incidentals")) or 0.0
    notes           = (request.form.get("notes") or "").strip() or None
    rate_type       = request.form.get("rate_type") if request.form.get("rate_type") in ("hourly", "flat") else "hourly"

    job_id = request.form.get("job_id")
    try:
        job_id = int(job_id) if job_id else None
    except (TypeError, ValueError):
        job_id = None

    import json as _json
    extra_lines = parse_extra_lines(
        request.form,
        ("ci_extra_diff_", "ci_extra_dur_", "ci_extra_amt_",
         "ci_extra_date_", "ci_extra_code_"),
        company, main_date=date_of_service)
    line_items = _json.dumps(extra_lines) if extra_lines else None
    extra_total = sum(l["amount"] for l in extra_lines)

    # Phase 5 (2026-07-22 batch, resolves 2026-06-20's open #15): the main
    # line's differential is wired directly into the applied rate, not just
    # added as an extra line — base_rate stays the client's raw rate,
    # rate_per_hour keeps its existing meaning of "the rate this bills at"
    # (base_rate + differential), mirroring invoices.base_rate/rate_applied
    # on the interpreter side.
    if rate_type == "hourly":
        # base_rate is the current field name; rate_per_hour is accepted too
        # for backward compatibility with callers built against the old form.
        base_rate_raw = (request.form.get("base_rate") or request.form.get("rate_per_hour") or "").strip()
        diff_raw = (request.form.get("differential") or "").strip()
        base_rate = _float_or_none(base_rate_raw)
        diff_val = _float_or_none(diff_raw) or 0.0

        # Blank base rate resolves from the client's rate effective on the
        # service date.
        if base_rate is None and client_id:
            base_rate = portal_rates.rate_for(client_id, date_of_service)
        if base_rate is None:
            return render_template("portal_client_invoice_create.html",
                                   clients=clients, error="Base rate is required.",
                                   diff_options_json=diff_options_json(company))

        rate_applied = base_rate + diff_val
        total = None
        if duration_hours:
            total = round(duration_hours * rate_applied + incidentals + extra_total, 2)

        cols_extra = {
            "rate_type": "hourly", "base_rate": base_rate,
            "differential": diff_raw or None, "rate_per_hour": rate_applied,
        }
    else:
        flat_amount = _float_or_none(request.form.get("flat_amount"))
        if flat_amount is None:
            return render_template("portal_client_invoice_create.html",
                                   clients=clients, error="Flat amount is required.",
                                   diff_options_json=diff_options_json(company))
        total = round(flat_amount + incidentals + extra_total, 2)
        cols_extra = {
            "rate_type": "flat", "base_rate": None,
            "differential": None, "rate_per_hour": None,
        }

    portal_db.execute(
        "INSERT INTO client_invoices "
        "(company, client_id, client_name, poc_email, poc_phone, date_of_service, "
        "start_time, end_time, duration_hours, rate_per_hour, incidentals, total, "
        "notes, line_items, job_id, created_by, submitted, rate_type, base_rate, differential) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,FALSE,%s,%s,%s)",
        (company, client_id, client_name, poc_email, poc_phone, date_of_service,
         start_time, end_time, duration_hours, cols_extra["rate_per_hour"], incidentals, total,
         notes, line_items, job_id, int(g.user["sub"]),
         cols_extra["rate_type"], cols_extra["base_rate"], cols_extra["differential"]),
    )
    audit_log("client_invoice_create",
              metadata={"client_name": client_name, "total": total, "rate_type": rate_type})
    flash("Client invoice created — pending review before the client can see it.", "success")
    return redirect("/portal/admin/client-review")


@client_inv_bp.route("/portal/admin/client-review")
@admin_required
def client_review():
    """Admin review queue: every client invoice still in draft (not yet
    submitted), newest first — the gate before a client can see it. Mirrors
    the Interpreter Review page's layout."""
    company = g.user["company"]
    invoices = portal_db.query_all(
        "SELECT ci.*, u.full_name AS user_full_name, j.job_number "
        "FROM client_invoices ci LEFT JOIN portal_users u ON u.id = ci.client_id "
        "LEFT JOIN jobs j ON j.id = ci.job_id "
        "WHERE ci.company = %s AND COALESCE(ci.submitted, FALSE) = FALSE "
        "ORDER BY ci.created_at DESC",
        (company,),
    )
    return render_template("portal_client_review.html", invoices=invoices)


@client_inv_bp.route("/portal/admin/client-invoices/submit-batch", methods=["POST"])
@admin_required
def submit_client_invoices_batch():
    """Admin selects draft client invoices from the Client Review page and
    submits them all at once — from that point the client can see them."""
    company = g.user["company"]
    ids_raw = request.form.getlist("invoice_ids")
    try:
        ids = [int(i) for i in ids_raw]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        flash("Select at least one invoice to submit.", "error")
        return redirect("/portal/admin/client-review")

    rows = portal_db.query_all(
        "SELECT id FROM client_invoices "
        "WHERE id = ANY(%s) AND company = %s AND COALESCE(submitted, FALSE) = FALSE",
        (ids, company),
    )
    if not rows:
        flash("Nothing eligible to submit.", "error")
        return redirect("/portal/admin/client-review")

    submitted_ids = [r["id"] for r in rows]
    portal_db.execute(
        "UPDATE client_invoices SET submitted = TRUE, submitted_at = NOW() WHERE id = ANY(%s)",
        (submitted_ids,),
    )
    audit_log("client_invoice_submit_batch", metadata={"invoice_ids": submitted_ids})
    flash(f"{len(submitted_ids)} invoice(s) submitted — now visible to the client.", "success")
    return redirect("/portal/admin/client-review")


@client_inv_bp.route("/portal/admin/client-invoices/<int:inv_id>/mark-paid",
                     methods=["POST"])
@admin_required
def mark_client_invoice_paid(inv_id):
    company = g.user["company"]
    inv = portal_db.query_one(
        "SELECT id FROM client_invoices WHERE id = %s AND company = %s",
        (inv_id, company),
    )
    if not inv:
        abort(404)
    paid_date = request.form.get("paid_date") or None
    portal_db.execute(
        "UPDATE client_invoices SET status='paid', paid_date=%s WHERE id=%s",
        (paid_date, inv_id),
    )
    audit_log("client_invoice_paid", target=f"client_invoice:{inv_id}",
              metadata={"paid_date": paid_date})
    flash("Invoice marked as paid.", "success")
    return redirect(f"/portal/client-invoices/{inv_id}")


@client_inv_bp.route("/portal/admin/client-invoices/<int:inv_id>/mark-unpaid",
                     methods=["POST"])
@admin_required
def mark_client_invoice_unpaid(inv_id):
    company = g.user["company"]
    inv = portal_db.query_one(
        "SELECT id FROM client_invoices WHERE id = %s AND company = %s",
        (inv_id, company),
    )
    if not inv:
        abort(404)
    portal_db.execute(
        "UPDATE client_invoices SET status='unpaid', paid_date=NULL WHERE id=%s",
        (inv_id,),
    )
    audit_log("client_invoice_unpaid", target=f"client_invoice:{inv_id}")
    flash("Invoice marked as unpaid.", "success")
    return redirect(f"/portal/client-invoices/{inv_id}")
