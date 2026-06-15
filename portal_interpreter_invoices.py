"""Interpreter master invoice — Phase 6 (Rose SLI portal).

Interpreters review their own confirmed/completed assignments (line items are
auto-built from each job's duration x the interpreter's rate), select which to
bill, and submit them as ONE master invoice. Submitting flips the master invoice
to 'submitted_for_payment' and emails the coordinator(s); no external system.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request

import portal_db
import portal_email
from portal_audit import log as audit_log
from portal_auth import login_required

interp_inv_bp = Blueprint("interpreter_invoices", __name__)

_COMPANY_NAMES = {"rosesli": "Rose Sign Language Interpreting", "dod": "DOD Cyber Consulting"}


def _company_name(company: str) -> str:
    return _COMPANY_NAMES.get(company, company)


def _abs_url(path: str) -> str:
    return request.url_root.rstrip("/") + path


def _float_or_none(raw):
    raw = (raw or "").strip() if isinstance(raw, str) else raw
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _line_amount(duration, rate):
    """duration_hours x rate, rounded. None when either is missing/non-numeric."""
    dur = _float_or_none(str(duration) if duration is not None else None)
    rt = _float_or_none(str(rate) if rate is not None else None)
    if dur is None or rt is None:
        return None
    return round(dur * rt, 2)


def billable_jobs_for_interpreter(company, uid):
    """Confirmed/completed assignments for this interpreter that have not yet
    been rolled into any master invoice. Each row gets a computed `line_amount`
    (duration x the interpreter's rate)."""
    rate = portal_db.query_one(
        "SELECT interpreter_rate FROM portal_users WHERE id = %s", (uid,),
    )
    rate = rate.get("interpreter_rate") if rate else None

    rows = portal_db.query_all(
        "SELECT id, job_number, event_date, start_time, end_time, setting, "
        "       duration, event_address, status "
        "FROM jobs "
        "WHERE company = %s "
        "  AND (interpreter_1_id = %s OR interpreter_2_id = %s) "
        "  AND status IN ('confirmed', 'completed') "
        "  AND id NOT IN (SELECT job_id FROM invoice_jobs WHERE job_id IS NOT NULL) "
        "ORDER BY event_date NULLS LAST, start_time",
        (company, uid, uid),
    )
    for r in rows:
        r["rate"] = rate
        r["line_amount"] = _line_amount(r.get("duration"), rate)
    return rows


def create_master_invoice(company, uid, job_ids, created_by=None):
    """Bundle the interpreter's selected billable assignments into one master
    invoice (status 'submitted_for_payment'). Re-queries billable jobs so only
    assignments that genuinely belong to this interpreter and are not already
    invoiced are billed — submitted ids are filtered against that set, so this
    can never double-bill or bill someone else's work. Returns the new invoice
    id, or None when nothing valid was selected.
    """
    try:
        wanted = {int(j) for j in job_ids}
    except (TypeError, ValueError):
        return None
    if not wanted:
        return None

    billable = {r["id"]: r for r in billable_jobs_for_interpreter(company, uid)}
    selected = [billable[jid] for jid in wanted if jid in billable]
    if not selected:
        return None

    total = round(sum(r["line_amount"] or 0 for r in selected), 2)
    label = f"Master invoice — {len(selected)} assignment{'s' if len(selected) != 1 else ''}"

    row = portal_db.execute(
        "INSERT INTO invoices "
        "(user_id, amount, description, status, submitted, submitted_at) "
        "VALUES (%s, %s, %s, 'submitted_for_payment', TRUE, NOW()) RETURNING id",
        (uid, total, label),
    )
    invoice_id = row["id"] if row else None
    if not invoice_id:
        return None

    for r in selected:
        portal_db.execute(
            "INSERT INTO invoice_jobs (invoice_id, job_id, company, line_amount) "
            "VALUES (%s, %s, %s, %s)",
            (invoice_id, r["id"], company, r["line_amount"]),
        )

    audit_log("master_invoice_submit", target=f"invoice:{invoice_id}",
              metadata={"total": total, "jobs": [r["id"] for r in selected]})
    return invoice_id


def _notify_admins(company, interpreter_name, invoice_id, total, selected):
    admins = portal_db.query_all(
        "SELECT email FROM portal_users "
        "WHERE company = %s AND role = 'admin' AND active = TRUE AND email IS NOT NULL",
        (company,),
    )
    lines = []
    for r in selected:
        when = r.get("event_date")
        when = when.strftime("%b %d, %Y") if hasattr(when, "strftime") else (when or "—")
        amt = r.get("line_amount")
        amt = f"${amt:.2f}" if amt is not None else "—"
        lines.append(f"{when}  {r.get('setting') or 'Assignment'}  {amt}")
    link = _abs_url(f"/portal/invoices/{invoice_id}")
    for a in admins:
        portal_email.send_master_invoice_email(
            a["email"], interpreter_name, invoice_id, total, lines, link,
            _company_name(company),
        )


@interp_inv_bp.route("/portal/pay/review")
@login_required
def pay_review():
    """Interpreter reviews their unbilled assignments and selects what to submit."""
    if g.user["role"] != "employee":
        abort(403)
    jobs = billable_jobs_for_interpreter(g.user["company"], int(g.user["sub"]))
    total = round(sum(j["line_amount"] or 0 for j in jobs), 2)
    return render_template("portal_pay_review.html", jobs=jobs, total=total)


@interp_inv_bp.route("/portal/pay/submit", methods=["POST"])
@login_required
def pay_submit():
    if g.user["role"] != "employee":
        abort(403)
    company = g.user["company"]
    uid     = int(g.user["sub"])
    job_ids = request.form.getlist("job_ids")
    if not job_ids:
        flash("Select at least one assignment to submit.", "error")
        return redirect("/portal/pay/review")

    # Snapshot the selected lines before they're consumed, for the email.
    billable = {r["id"]: r for r in billable_jobs_for_interpreter(company, uid)}
    selected = []
    for raw in job_ids:
        try:
            jid = int(raw)
        except (TypeError, ValueError):
            continue
        if jid in billable:
            selected.append(billable[jid])

    invoice_id = create_master_invoice(company, uid, job_ids, created_by=uid)
    if not invoice_id:
        flash("No billable assignments were selected.", "error")
        return redirect("/portal/pay/review")

    total = round(sum(r["line_amount"] or 0 for r in selected), 2)
    try:
        _notify_admins(company, g.user.get("name") or g.user.get("email") or "An interpreter",
                       invoice_id, total, selected)
    except Exception:
        pass  # email failure must not lose the submitted invoice

    flash("Invoice submitted for payment.", "success")
    return redirect(f"/portal/invoices/{invoice_id}")
