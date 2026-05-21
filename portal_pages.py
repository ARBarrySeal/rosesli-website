import os
from flask import Blueprint, g, jsonify, redirect, render_template, request

import portal_db
from portal_auth import admin_required, check_password, hash_password, login_required

pages_bp = Blueprint("pages", __name__)

CALENDLY_URLS = {
    "dod":      "https://calendly.com/rosecharlesrose",
    "rosesli":  "https://calendly.com/rosecharlesrose",
}


@pages_bp.route("/portal")
@login_required
def dashboard():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        stats = {
            "users": portal_db.query_one(
                "SELECT COUNT(*) AS n FROM portal_users WHERE company = %s AND active = TRUE",
                (company,),
            )["n"],
            "invoices": portal_db.query_one(
                "SELECT COUNT(*) AS n FROM invoices i "
                "JOIN portal_users u ON u.id = i.user_id WHERE u.company = %s",
                (company,),
            )["n"],
            "pending": portal_db.query_one(
                "SELECT COUNT(*) AS n FROM invoices i "
                "JOIN portal_users u ON u.id = i.user_id "
                "WHERE u.company = %s AND i.status = 'unpaid'",
                (company,),
            )["n"],
        }
        invoices = portal_db.query_all(
            "SELECT i.id, u.full_name, u.email, i.amount, i.status, i.due_date "
            "FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "WHERE u.company = %s ORDER BY i.created_at DESC LIMIT 10",
            (company,),
        )
    else:
        stats = {
            "invoices": portal_db.query_one(
                "SELECT COUNT(*) AS n FROM invoices WHERE user_id = %s", (uid,),
            )["n"],
            "pending": portal_db.query_one(
                "SELECT COUNT(*) AS n FROM invoices WHERE user_id = %s AND status = 'unpaid'",
                (uid,),
            )["n"],
            "bookings": portal_db.query_one(
                "SELECT COUNT(*) AS n FROM bookings WHERE user_id = %s AND datetime > NOW()",
                (uid,),
            )["n"],
        }
        invoices = portal_db.query_all(
            "SELECT id, amount, status, due_date FROM invoices "
            "WHERE user_id = %s ORDER BY created_at DESC LIMIT 5",
            (uid,),
        )

    return render_template("portal_dashboard.html", stats=stats, invoices=invoices)


@pages_bp.route("/portal/profile", methods=["GET", "POST"])
@login_required
def profile():
    uid     = g.user["sub"]
    user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (uid,))
    error   = None
    success = None

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update":
            full_name    = (request.form.get("full_name")    or "").strip()
            phone        = (request.form.get("phone")        or "").strip()
            company_name = (request.form.get("company_name") or "").strip()
            address      = (request.form.get("address")      or "").strip()
            if not full_name:
                error = "Full name is required."
            else:
                portal_db.execute(
                    "UPDATE portal_users SET full_name=%s, phone=%s, company_name=%s, address=%s "
                    "WHERE id=%s",
                    (full_name, phone, company_name, address, uid),
                )
                user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (uid,))
                success = "Profile updated."

        elif action == "password":
            current = request.form.get("current_password") or ""
            new_pw  = request.form.get("new_password")     or ""
            confirm = request.form.get("confirm_password") or ""
            if not user["password_hash"] or not check_password(current, user["password_hash"]):
                error = "Current password is incorrect."
            elif len(new_pw) < 8:
                error = "New password must be at least 8 characters."
            elif new_pw != confirm:
                error = "New passwords do not match."
            else:
                portal_db.execute(
                    "UPDATE portal_users SET password_hash=%s WHERE id=%s",
                    (hash_password(new_pw), uid),
                )
                success = "Password updated."

    return render_template("portal_profile.html", user=user, error=error, success=success)


@pages_bp.route("/portal/schedule")
@login_required
def schedule():
    company      = g.user["company"]
    calendly_url = CALENDLY_URLS.get(company, "https://calendly.com/rosecharlesrose")
    n8n_base     = os.environ.get("N8N_BASE", "http://localhost:5678")
    n8n_webhook  = f"{n8n_base}/webhook/leads"
    return render_template("portal_schedule.html", calendly_url=calendly_url, n8n_webhook=n8n_webhook)


@pages_bp.route("/portal/admin/users")
@admin_required
def admin_users():
    company = g.user["company"]
    users   = portal_db.query_all(
        "SELECT id, email, full_name, role, active, created_at "
        "FROM portal_users WHERE company = %s ORDER BY created_at DESC",
        (company,),
    )
    return render_template("portal_admin_users.html", users=users)


@pages_bp.route("/portal/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def toggle_user(user_id):
    company = g.user["company"]
    user    = portal_db.query_one(
        "SELECT id, active, role FROM portal_users WHERE id = %s AND company = %s",
        (user_id, company),
    )
    if not user:
        return jsonify(ok=False, error="User not found"), 404
    if user["role"] == "admin":
        return jsonify(ok=False, error="Cannot deactivate admin accounts"), 400
    new_state = not user["active"]
    portal_db.execute(
        "UPDATE portal_users SET active = %s WHERE id = %s",
        (new_state, user_id),
    )
    return jsonify(ok=True, active=new_state)
