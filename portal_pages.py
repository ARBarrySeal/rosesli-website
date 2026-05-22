import mimetypes
import os
import uuid

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file
from werkzeug.utils import secure_filename

import portal_db
from portal_auth import admin_required, check_password, hash_password, login_required

UPLOAD_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".gif",
    ".txt", ".csv", ".zip",
}

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
            full_name    = (request.form.get("full_name")    or "").strip()[:255]
            phone        = (request.form.get("phone")        or "").strip()[:50]
            company_name = (request.form.get("company_name") or "").strip()[:255]
            address      = (request.form.get("address")      or "").strip()[:500]
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


# ── Invoices ──────────────────────────────────────────────────────────────────

@pages_bp.route("/portal/invoices")
@login_required
def invoices():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        rows = portal_db.query_all(
            "SELECT i.id, u.full_name, u.email, i.amount, i.status, i.due_date, i.created_at "
            "FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "WHERE u.company = %s ORDER BY i.created_at DESC",
            (company,),
        )
    else:
        rows = portal_db.query_all(
            "SELECT id, amount, status, due_date, created_at "
            "FROM invoices WHERE user_id = %s ORDER BY created_at DESC",
            (uid,),
        )
    return render_template("portal_invoices.html", invoices=rows)


@pages_bp.route("/portal/invoices/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        inv = portal_db.query_one(
            "SELECT i.*, u.full_name, u.email FROM invoices i "
            "JOIN portal_users u ON u.id = i.user_id "
            "WHERE i.id = %s AND u.company = %s",
            (invoice_id, company),
        )
    else:
        inv = portal_db.query_one(
            "SELECT * FROM invoices WHERE id = %s AND user_id = %s",
            (invoice_id, uid),
        )

    if not inv:
        abort(404)
    return render_template("portal_invoice.html", inv=inv)


@pages_bp.route("/portal/admin/invoices/<int:invoice_id>/mark-paid", methods=["POST"])
@admin_required
def mark_invoice_paid(invoice_id):
    company = g.user["company"]
    inv = portal_db.query_one(
        "SELECT i.id FROM invoices i JOIN portal_users u ON u.id = i.user_id "
        "WHERE i.id = %s AND u.company = %s",
        (invoice_id, company),
    )
    if not inv:
        abort(404)
    portal_db.execute("UPDATE invoices SET status='paid' WHERE id=%s", (invoice_id,))
    flash("Invoice marked as paid.", "success")
    return redirect(f"/portal/invoices/{invoice_id}")


# ── Documents ─────────────────────────────────────────────────────────────────

@pages_bp.route("/portal/documents")
@login_required
def documents():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]
    if role == "admin":
        docs = portal_db.query_all(
            "SELECT d.*, u.full_name, u.email FROM portal_documents d "
            "JOIN portal_users u ON u.id = d.user_id "
            "WHERE d.company = %s ORDER BY d.created_at DESC",
            (company,),
        )
        users = portal_db.query_all(
            "SELECT id, full_name, email FROM portal_users "
            "WHERE company = %s AND active = TRUE ORDER BY full_name",
            (company,),
        )
    else:
        docs = portal_db.query_all(
            "SELECT * FROM portal_documents WHERE user_id = %s ORDER BY created_at DESC",
            (uid,),
        )
        users = []
    return render_template("portal_documents.html", docs=docs, users=users)


@pages_bp.route("/portal/documents/upload", methods=["POST"])
@login_required
def upload_document():
    uid     = g.user["sub"]
    company = g.user["company"]
    role    = g.user["role"]

    if "file" not in request.files:
        flash("No file selected.", "error")
        return redirect("/portal/documents")

    f = request.files["file"]
    if not f.filename:
        flash("No file selected.", "error")
        return redirect("/portal/documents")

    _, ext = os.path.splitext(secure_filename(f.filename))
    if ext.lower() not in ALLOWED_EXTENSIONS:
        flash("File type not allowed.", "error")
        return redirect("/portal/documents")

    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > MAX_UPLOAD_BYTES:
        flash("File too large (25 MB max).", "error")
        return redirect("/portal/documents")

    target_uid = uid
    if role == "admin":
        raw = (request.form.get("target_user_id") or "").strip()
        if raw:
            try:
                target_row = portal_db.query_one(
                    "SELECT id FROM portal_users WHERE id = %s AND company = %s",
                    (int(raw), company),
                )
                if target_row:
                    target_uid = target_row["id"]
            except (ValueError, TypeError):
                pass

    stored_name = uuid.uuid4().hex + ext.lower()
    company_dir = os.path.join(UPLOAD_BASE, company)
    os.makedirs(company_dir, exist_ok=True)
    f.save(os.path.join(company_dir, stored_name))

    mime = f.content_type or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
    portal_db.execute(
        "INSERT INTO portal_documents "
        "(user_id, company, filename, original_name, mime_type, size_bytes, uploaded_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (target_uid, company, stored_name, secure_filename(f.filename), mime, size, uid),
    )
    flash("File uploaded.", "success")
    return redirect("/portal/documents")


@pages_bp.route("/portal/documents/<int:doc_id>/download")
@login_required
def download_document(doc_id):
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        doc = portal_db.query_one(
            "SELECT * FROM portal_documents WHERE id = %s AND company = %s",
            (doc_id, company),
        )
    else:
        doc = portal_db.query_one(
            "SELECT * FROM portal_documents WHERE id = %s AND user_id = %s",
            (doc_id, uid),
        )

    if not doc:
        abort(404)

    file_path = os.path.join(UPLOAD_BASE, company, doc["filename"])
    if not os.path.isfile(file_path):
        abort(404)

    return send_file(file_path, download_name=doc["original_name"], as_attachment=True)


@pages_bp.route("/portal/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        doc = portal_db.query_one(
            "SELECT * FROM portal_documents WHERE id = %s AND company = %s",
            (doc_id, company),
        )
    else:
        doc = portal_db.query_one(
            "SELECT * FROM portal_documents WHERE id = %s AND user_id = %s",
            (doc_id, uid),
        )

    if not doc:
        abort(404)

    try:
        os.remove(os.path.join(UPLOAD_BASE, company, doc["filename"]))
    except OSError:
        pass

    portal_db.execute("DELETE FROM portal_documents WHERE id = %s", (doc_id,))
    flash("File deleted.", "success")
    return redirect("/portal/documents")
