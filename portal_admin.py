import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, g, jsonify, render_template, request

import portal_db
from portal_audit import log as audit_log
from portal_auth import admin_required, login_required, make_token
from portal_email import send_invite_email, send_test_email

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

    app_url   = os.environ.get("APP_URL", "http://localhost:8080")
    setup_url = f"{app_url}/setup-account/{token}"
    send_invite_email(email, full_name, setup_url, COMPANY_NAMES.get(company, company))

    return render_template("portal_admin_invite.html", sent=True, email=email,
                           home_url="/")


@admin_bp.route("/portal/admin/invoices/create", methods=["GET", "POST"])
@login_required
def create_invoice():
    role = g.user["role"]
    if role == "client":
        abort(403)
    company  = g.user["company"]
    is_admin = role == "admin"
    # Admins create pay invoices for interpreters; interpreters submit their own.
    interpreters = portal_db.query_all(
        "SELECT id, full_name, email, interpreter_rate FROM portal_users "
        "WHERE company = %s AND role = 'employee' AND active = TRUE ORDER BY full_name",
        (company,),
    ) if is_admin else []

    if request.method == "GET":
        return render_template("portal_admin_invoice_create.html", interpreters=interpreters)

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

    # Collect dynamic extra lines
    import json as _json
    extra_lines = []
    idx = 0
    while True:
        ed = request.form.get(f"extra_differential_{idx}")
        if ed is None:
            break
        try:
            ea = float(request.form.get(f"extra_amount_{idx}") or 0)
        except ValueError:
            ea = 0.0
        try:
            edur = float(request.form.get(f"extra_duration_{idx}") or 0)
        except ValueError:
            edur = 0.0
        extra_lines.append({"differential": ed, "duration": edur, "amount": ea})
        idx += 1
    interpreter_rates = _json.dumps(extra_lines) if extra_lines else None

    if is_admin:
        user_id = request.form.get("user_id") or ""
        if not user_id:
            return render_template("portal_admin_invoice_create.html", interpreters=interpreters,
                                   error="Please select an interpreter.")
        target = portal_db.query_one(
            "SELECT id FROM portal_users WHERE id = %s AND company = %s AND role = 'employee'",
            (int(user_id), company),
        )
        if not target:
            return render_template("portal_admin_invoice_create.html", interpreters=interpreters,
                                   error="Invalid interpreter.")
        recipient_id = int(user_id)
    else:
        recipient_id = int(g.user["sub"])

    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return render_template("portal_admin_invoice_create.html", interpreters=interpreters,
                               error="Amount must be a positive number.")

    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, description, due_date, interpreter_rates, notes, "
        "  date_of_service, service_start_time, service_end_time, duration_hours, "
        "  base_rate, differential, rate_applied) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (recipient_id, amount, description or None, due_date or None,
         interpreter_rates, notes or None,
         date_of_service or None, start_time, end_time, duration_hours,
         base_rate, diff_raw or None, rate_applied),
    )
    audit_log(
        "invoice_create",
        target=f"user:{recipient_id}",
        metadata={"amount": amount, "due_date": due_date or None, "self_submitted": not is_admin},
    )
    return render_template("portal_admin_invoice_create.html", interpreters=interpreters, success=True)


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
