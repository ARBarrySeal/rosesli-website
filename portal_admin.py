import os
from datetime import datetime, timedelta, timezone

import psycopg2
from flask import (
    Blueprint, abort, flash, g, jsonify, redirect, render_template, request,
)

import portal_db
import portal_rates
from portal_audit import log as audit_log
from portal_auth import (
    DEFAULT_PASSWORD, admin_required, hash_password, login_required, make_token,
)
from portal_email import (
    send_default_password_email, send_invite_email, send_test_email,
)

admin_bp = Blueprint("admin", __name__)

COMPANY_NAMES = {
    "dod": "DoD Cyber Consulting",
    "rosesli": "Rose Sign Language Interpreting",
}


@admin_bp.route("/portal/admin/invite", methods=["GET", "POST"])
@admin_required
def invite():
    company = g.user["company"]

    if request.method == "GET":
        return render_template("portal_admin_invite.html")

    email     = (request.form.get("email")     or "").strip().lower()
    full_name = (request.form.get("full_name") or "").strip()
    role      = request.form.get("role") or "client"

    if role not in ("client", "employee"):
        return render_template("portal_admin_invite.html", error="Invalid role.")
    if not email or not full_name:
        return render_template("portal_admin_invite.html", error="Email and name are required.")

    existing = portal_db.query_one(
        "SELECT id FROM portal_users WHERE email = %s AND company = %s",
        (email, company),
    )
    if existing:
        return render_template("portal_admin_invite.html",
                               error="That email already has an account.")

    app_url = os.environ.get("APP_URL", "http://localhost:8080")

    if company == "rosesli":
        # Rose SLI flow: account is created live with the default password;
        # first sign-in forces the user to create their own password.
        portal_db.execute(
            "INSERT INTO portal_users (email, full_name, role, company, "
            "                          password_hash, must_change_password, active) "
            "VALUES (%s, %s, %s, %s, %s, TRUE, TRUE)",
            (email, full_name, role, company, hash_password(DEFAULT_PASSWORD)),
        )
        new_user = portal_db.query_one(
            "SELECT id FROM portal_users WHERE email = %s AND company = %s",
            (email, company),
        )
        audit_log(
            "invite_send",
            target=f"user:{new_user['id']}" if new_user else None,
            metadata={"email": email, "role": role, "full_name": full_name,
                      "flow": "default_password"},
        )
        send_default_password_email(email, full_name, f"{app_url}/login",
                                    COMPANY_NAMES.get(company, company))
        return render_template("portal_admin_invite.html", sent=True, email=email,
                               home_url="/", default_pw_flow=True)

    token   = make_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=48)

    portal_db.execute(
        "INSERT INTO portal_users (email, full_name, role, company, invite_token, invite_expires, active) "
        "VALUES (%s, %s, %s, %s, %s, %s, FALSE)",
        (email, full_name, role, company, token, expires),
    )

    new_user = portal_db.query_one(
        "SELECT id FROM portal_users WHERE email = %s AND company = %s",
        (email, company),
    )
    audit_log(
        "invite_send",
        target=f"user:{new_user['id']}" if new_user else None,
        metadata={"email": email, "role": role, "full_name": full_name},
    )

    setup_url = f"{app_url}/setup-account/{token}"
    send_invite_email(email, full_name, setup_url, COMPANY_NAMES.get(company, company))

    return render_template("portal_admin_invite.html", sent=True, email=email,
                           home_url="/")


EXPENSE_CATEGORIES = ["Parking", "Mileage", "Travel Time", "Other"]


def parse_expenses(form):
    """Collect the dynamic Expenses lines from an invoice create/edit post:
    expense_category_N (Parking/Mileage/Travel Time/Other) + expense_note_N free
    text. Informational only — never priced or rolled into the invoice amount
    (2026-07-23 decision). Blank category rows are dropped."""
    expenses = []
    idx = 0
    while True:
        cat = form.get(f"expense_category_{idx}")
        if cat is None:
            break
        cat = cat.strip()
        if cat:
            note = (form.get(f"expense_note_{idx}") or "").strip()
            expenses.append({"category": cat, "note": note})
        idx += 1
    return expenses


@admin_bp.route("/portal/api/time-bands")
@login_required
def time_bands_json():
    """Split a shift into per-differential-band hours (day/evening/overnight x
    weekday/weekend), for auto-generating invoice rate lines from a date+time
    range. Returns {} for missing/invalid input rather than erroring, since the
    form calls this on every keystroke while the fields are still incomplete."""
    from datetime import time as _time
    raw_date = request.args.get("date") or ""
    raw_start = request.args.get("start") or ""
    raw_end = request.args.get("end") or ""
    try:
        event_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        start_time = _time.fromisoformat(raw_start)
        end_time = _time.fromisoformat(raw_end)
        bands = portal_rates.compute_time_band_hours(event_date, start_time, end_time)
    except (ValueError, TypeError):
        bands = {}
    return jsonify(bands=bands)


@admin_bp.route("/portal/admin/invoices/create", methods=["GET", "POST"])
@admin_bp.route("/portal/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def create_invoice(invoice_id=None):
    import json as _json

    role = g.user["role"]
    if role == "client":
        abort(403)
    company  = g.user["company"]
    uid      = int(g.user["sub"])
    is_admin = role == "admin"

    inv = None
    if invoice_id is not None:
        inv = portal_db.query_one(
            "SELECT i.* FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "WHERE i.id = %s AND u.company = %s",
            (invoice_id, company),
        )
        if not inv:
            abort(404)
        if not is_admin and inv["user_id"] != uid:
            abort(403)
        if not is_admin and inv["submitted"]:
            flash("This invoice was submitted for review and can no longer be edited.", "error")
            return redirect(f"/portal/invoices/{invoice_id}")

    # Admins bill any interpreter; interpreters submit their own.
    interpreters = portal_db.query_all(
        "SELECT id, full_name, email, interpreter_rate FROM portal_users "
        "WHERE company = %s AND role = 'employee' AND active = TRUE ORDER BY full_name",
        (company,),
    ) if is_admin else []

    from portal_client_invoices import diff_options_json, parse_extra_lines
    diffs_json = diff_options_json(company, label_style="admin")

    # Interpreters creating a fresh invoice can link it to one of their own
    # unbilled assignments — this auto-fills date/time and, once saved, keeps
    # that job out of the /portal/pay master-invoice list (job_id unique index,
    # migration 019) so the same work is never billed through both paths.
    billable_jobs = []
    if not is_admin and invoice_id is None:
        from portal_interpreter_invoices import billable_jobs_for_interpreter
        billable_jobs = billable_jobs_for_interpreter(company, uid)

    if request.method == "GET":
        return render_template("portal_admin_invoice_create.html",
                               interpreters=interpreters, diff_options_json=diffs_json,
                               inv=inv, company_id=company, billable_jobs=billable_jobs,
                               expense_categories=EXPENSE_CATEGORIES)

    amount_raw      = (request.form.get("amount") or "").strip()
    description     = (request.form.get("description") or "").strip()
    due_date        = request.form.get("due_date") or None
    notes           = (request.form.get("notes") or "").strip()
    date_of_service = request.form.get("date_of_service") or None
    start_time      = (request.form.get("start_time") or "").strip() or None
    end_time        = (request.form.get("end_time") or "").strip() or None
    base_rate_raw   = (request.form.get("base_rate") or "").strip()
    diff_raw        = (request.form.get("differential") or "0").strip()
    dur_raw         = (request.form.get("duration_hours") or "").strip()
    try:
        base_rate = float(base_rate_raw) if base_rate_raw else None
    except ValueError:
        base_rate = None
    try:
        diff_val = float(diff_raw) if diff_raw else 0.0
    except ValueError:
        diff_val = 0.0
    try:
        duration_hours = float(dur_raw) if dur_raw else None
    except ValueError:
        duration_hours = None
    rate_applied = (base_rate or 0) + diff_val if base_rate is not None else None

    # Collect dynamic extra (differential) lines and Expenses lines.
    extra_lines = parse_extra_lines(
        request.form,
        ("extra_differential_", "extra_duration_", "extra_amount_",
         "extra_date_", "extra_code_"),
        company, main_date=date_of_service)
    interpreter_rates = _json.dumps(extra_lines) if extra_lines else None

    expense_lines = parse_expenses(request.form)
    expenses = _json.dumps(expense_lines) if expense_lines else None

    def _rerender(error):
        return render_template("portal_admin_invoice_create.html", interpreters=interpreters,
                               diff_options_json=diffs_json, inv=inv, company_id=company,
                               billable_jobs=billable_jobs,
                               expense_categories=EXPENSE_CATEGORIES, error=error)

    if is_admin:
        user_id = request.form.get("user_id") or (str(inv["user_id"]) if inv else "")
        if not user_id:
            return _rerender("Please select an interpreter.")
        target = portal_db.query_one(
            "SELECT id FROM portal_users WHERE id = %s AND company = %s AND role = 'employee'",
            (int(user_id), company),
        )
        if not target:
            return _rerender("Invalid interpreter.")
        recipient_id = int(user_id)
    else:
        recipient_id = int(g.user["sub"])

    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return _rerender("Amount must be a positive number.")

    # Optional job link (new invoices, interpreter self-create only). Blank or
    # foreign job ids are ignored rather than erroring the whole submission.
    job_id = None
    if inv is None and not is_admin:
        raw_job_id = request.form.get("job_id") or ""
        if raw_job_id:
            try:
                candidate = int(raw_job_id)
            except ValueError:
                candidate = None
            if candidate is not None and any(j["id"] == candidate for j in billable_jobs):
                job_id = candidate

    if inv is not None:
        portal_db.execute(
            "UPDATE invoices SET user_id = %s, amount = %s, description = %s, due_date = %s, "
            "  interpreter_rates = %s, notes = %s, expenses = %s, "
            "  date_of_service = %s, service_start_time = %s, service_end_time = %s, "
            "  duration_hours = %s, base_rate = %s, differential = %s, rate_applied = %s "
            "WHERE id = %s",
            (recipient_id, amount, description or None, due_date or None,
             interpreter_rates, notes or None, expenses,
             date_of_service or None, start_time, end_time, duration_hours,
             base_rate, diff_raw or None, rate_applied, invoice_id),
        )
        audit_log(
            "invoice_update",
            target=f"invoice:{invoice_id}",
            metadata={"amount": amount, "due_date": due_date or None},
        )
        flash("Invoice updated.", "success")
        return redirect(f"/portal/invoices/{invoice_id}")

    try:
        new_row = portal_db.execute(
            "INSERT INTO invoices (user_id, amount, description, due_date, interpreter_rates, "
            "  notes, expenses, date_of_service, service_start_time, service_end_time, "
            "  duration_hours, base_rate, differential, rate_applied, job_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (recipient_id, amount, description or None, due_date or None,
             interpreter_rates, notes or None, expenses,
             date_of_service or None, start_time, end_time, duration_hours,
             base_rate, diff_raw or None, rate_applied, job_id),
        )
    except psycopg2.IntegrityError:
        return _rerender("That assignment already has an invoice.")

    audit_log(
        "invoice_create",
        target=f"user:{recipient_id}",
        metadata={"amount": amount, "due_date": due_date or None, "self_submitted": not is_admin},
    )
    return redirect(f"/portal/invoices/{new_row['id']}")


@admin_bp.route("/portal/admin/smtp-test", methods=["POST"])
@admin_required
def smtp_test():
    """Send a test email to the calling admin so they can verify the SMTP
    configuration actually delivers — invite + reset flows fail silently
    today if SMTP is misconfigured (send returns False, no surface)."""
    admin_email = g.user["email"]
    company = g.user["company"]
    ok, detail = send_test_email(admin_email, COMPANY_NAMES.get(company, company))
    audit_log(
        "smtp_test",
        metadata={"to": admin_email, "ok": ok, "detail": detail[:200]},
    )
    return jsonify(ok=ok, sent_to=admin_email, detail=detail)


@admin_bp.route("/portal/admin/differentials", methods=["GET", "POST"])
@admin_required
def differentials_settings():
    """Amanda self-serves differential pricing. Each save writes an
    effective-dated row (upsert on company+code+effective_date), so invoices
    keep resolving the amount in force on their service date — past invoices
    never reprice."""
    from datetime import date as _date
    company = g.user["company"]

    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        eff  = (request.form.get("effective_date") or "").strip()
        try:
            amount = float(request.form.get("amount") or "")
        except ValueError:
            amount = None
        try:
            _date.fromisoformat(eff)
        except ValueError:
            eff = None
        current = {r["code"]: r for r in portal_rates.differentials_for(
            company, include_specialty=True)}
        if code not in current or amount is None or amount < 0 or not eff:
            flash("Invalid differential update.", "error")
        else:
            row = current[code]
            portal_db.execute(
                "INSERT INTO differentials "
                "(company, code, label, amount, effective_date, sort_order) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (company, code, effective_date) "
                "DO UPDATE SET amount = EXCLUDED.amount",
                (company, code, row["label"], amount, eff, row["sort_order"]),
            )
            audit_log("differential_update",
                      metadata={"code": code, "amount": amount,
                                "effective_date": eff})
            flash(f"{row['label']} set to ${amount:g}/hr effective {eff}.",
                  "success")
        return redirect("/portal/admin/differentials")

    rows = portal_rates.differentials_for(company, include_specialty=True)
    upcoming = portal_db.query_all(
        "SELECT code, amount, effective_date FROM differentials "
        "WHERE company = %s AND active = TRUE AND effective_date > %s "
        "ORDER BY code, effective_date",
        (company, _date.today()),
    )
    return render_template("portal_admin_differentials.html",
                           rows=rows, upcoming=upcoming, today=_date.today())
