import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, render_template, request

import portal_db
from portal_auth import admin_required, make_token
from portal_email import send_invite_email

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

    app_url   = os.environ.get("APP_URL", "http://localhost:8080")
    setup_url = f"{app_url}/setup-account/{token}"
    send_invite_email(email, full_name, setup_url, COMPANY_NAMES.get(company, company))

    return render_template("portal_admin_invite.html", sent=True, email=email)


@admin_bp.route("/portal/admin/invoices/create", methods=["GET", "POST"])
@admin_required
def create_invoice():
    company = g.user["company"]
    users   = portal_db.query_all(
        "SELECT id, full_name, email FROM portal_users "
        "WHERE company = %s AND active = TRUE ORDER BY full_name",
        (company,),
    )

    if request.method == "GET":
        return render_template("portal_admin_invoice_create.html", users=users)

    user_id     = request.form.get("user_id") or ""
    amount_raw  = (request.form.get("amount") or "").strip()
    description = (request.form.get("description") or "").strip()
    due_date    = request.form.get("due_date") or None

    if not user_id:
        return render_template("portal_admin_invoice_create.html", users=users,
                               error="Please select a client.")

    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return render_template("portal_admin_invoice_create.html", users=users,
                               error="Amount must be a positive number.")

    target = portal_db.query_one(
        "SELECT id FROM portal_users WHERE id = %s AND company = %s",
        (int(user_id), company),
    )
    if not target:
        return render_template("portal_admin_invoice_create.html", users=users,
                               error="Invalid user.")

    portal_db.execute(
        "INSERT INTO invoices (user_id, amount, description, due_date) VALUES (%s, %s, %s, %s)",
        (int(user_id), amount, description or None, due_date or None),
    )
    return render_template("portal_admin_invoice_create.html", users=users, success=True)
