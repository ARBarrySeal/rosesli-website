import json
import mimetypes
import os
import uuid
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, abort, flash, g, jsonify, make_response,
    redirect, render_template, request,
)
from werkzeug.utils import secure_filename

import portal_db
import portal_mfa
import portal_storage
from portal_audit import log as audit_log
from portal_auth import (
    SESSION_HOURS, _password_too_weak, _set_session_cookie,
    admin_required, check_password, decode_jwt, encode_jwt,
    hash_password, login_required, rotate_csrf_token,
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".gif",
    ".txt", ".csv", ".zip",
}

# Magic bytes for binary formats — text formats (.txt, .csv) have no header to check.
_MAGIC: dict[str, list[bytes]] = {
    ".pdf":  [b"%PDF"],
    ".doc":  [b"\xD0\xCF\x11\xE0"],
    ".xls":  [b"\xD0\xCF\x11\xE0"],
    ".docx": [b"PK\x03\x04"],
    ".xlsx": [b"PK\x03\x04"],
    ".zip":  [b"PK\x03\x04"],
    ".png":  [b"\x89PNG"],
    ".jpg":  [b"\xFF\xD8\xFF"],
    ".jpeg": [b"\xFF\xD8\xFF"],
    ".gif":  [b"GIF87a", b"GIF89a"],
}


def _magic_ok(f, ext: str) -> bool:
    sigs = _MAGIC.get(ext.lower())
    if sigs is None:
        return True
    header = f.read(8)
    f.seek(0)
    return any(header.startswith(s) for s in sigs)

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
            weak = _password_too_weak(new_pw)
            if not user["password_hash"] or not check_password(current, user["password_hash"]):
                error = "Current password is incorrect."
            elif weak:
                error = weak
            elif new_pw != confirm:
                error = "New passwords do not match."
            else:
                portal_db.execute(
                    "UPDATE portal_users SET password_hash = %s, pw_changed_at = NOW(), "
                    "    failed_login_count = 0, locked_until = NULL "
                    "WHERE id = %s",
                    (hash_password(new_pw), uid),
                )
                audit_log("pw_change", user_id=uid, email=user["email"], company=user["company"])

                # Reissue JWT so iat advances past the pw_changed_at we just wrote
                # — without this, the @login_required check on the next request would
                # invalidate this device too, contradicting the success message.
                # _set_session_cookie also rotates the per-session CSRF token (P2c).
                fresh_token = encode_jwt({
                    "sub":     str(uid),
                    "email":   g.user["email"],
                    "role":    g.user["role"],
                    "company": g.user["company"],
                    "name":    g.user.get("name") or user["email"],
                    "mfa":     g.user.get("mfa", "ok"),
                })
                resp = make_response(render_template(
                    "portal_profile.html",
                    user=user,
                    error=None,
                    success="Password updated. You'll stay signed in here; other devices will be signed out.",
                ))
                _set_session_cookie(resp, fresh_token, max_age=SESSION_HOURS * 3600)
                return resp

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
        "SELECT id, email, active, role FROM portal_users WHERE id = %s AND company = %s",
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
    audit_log(
        "user_activate" if new_state else "user_deactivate",
        target=f"user:{user_id}",
        metadata={"email": user["email"]},
    )
    return jsonify(ok=True, active=new_state)


# ── MFA enrollment (admins) ───────────────────────────────────────────────────

@pages_bp.route("/portal/mfa-enroll", methods=["GET", "POST"])
def mfa_enroll():
    """Accessible by admins holding either a pending-enroll JWT or a full session."""
    token = request.cookies.get("portal_token")
    payload = decode_jwt(token) if token else None
    if not payload or payload.get("role") != "admin":
        return redirect("/")

    try:
        uid = int(payload["sub"])
    except (ValueError, TypeError):
        return redirect("/")

    user = portal_db.query_one(
        "SELECT id, email, active, mfa_secret FROM portal_users "
        "WHERE id = %s AND role = 'admin'",
        (uid,),
    )
    if not user or not user["active"]:
        return redirect("/")

    # Generate a fresh secret per-request *unless* we're confirming a code; in that
    # case we need the secret the user just scanned, which we carry via signed form
    # token.
    if request.method == "GET":
        secret = portal_mfa.new_secret()
        # Stash candidate secret in a short-lived JWT so we don't persist until confirmed.
        candidate = encode_jwt({"candidate_secret": secret, "for_uid": uid},
                               lifetime=timedelta(minutes=10))
        uri = portal_mfa.provisioning_uri(secret, user["email"])
        return render_template(
            "portal_mfa_enroll.html",
            qr_svg=portal_mfa.qr_svg(uri),
            secret=secret,
            candidate=candidate,
        )

    # POST — confirm
    candidate_jwt = request.form.get("candidate") or ""
    code = (request.form.get("code") or "").strip()
    cp = decode_jwt(candidate_jwt) if candidate_jwt else None
    if not cp or cp.get("for_uid") != uid or "candidate_secret" not in cp:
        return redirect("/portal/mfa-enroll")
    secret = cp["candidate_secret"]

    if not portal_mfa.verify_code(secret, code):
        uri = portal_mfa.provisioning_uri(secret, user["email"])
        return render_template(
            "portal_mfa_enroll.html",
            qr_svg=portal_mfa.qr_svg(uri),
            secret=secret,
            candidate=candidate_jwt,
            error="That code didn't match. Try again — codes rotate every 30 seconds.",
        ), 400

    recovery_codes = portal_mfa.new_recovery_codes()
    recovery_hashes = portal_mfa.hash_recovery_codes(recovery_codes)

    portal_db.execute(
        "UPDATE portal_users SET mfa_secret = %s, mfa_enrolled_at = NOW(), "
        "    mfa_recovery_hashes = %s::jsonb WHERE id = %s",
        (secret, json.dumps(recovery_hashes), uid),
    )
    audit_log("mfa_enroll", user_id=uid, email=user["email"], company=g.user.get("company") if hasattr(g, "user") else payload.get("company"))

    # Issue a full session token now that enrollment is complete.
    full_token = encode_jwt({
        "sub":     str(uid),
        "email":   user["email"],
        "role":    "admin",
        "company": payload.get("company"),
        "name":    payload.get("name") or user["email"],
        "mfa":     "ok",
    })
    resp = make_response(render_template(
        "portal_mfa_enroll.html",
        success=True,
        recovery_codes=recovery_codes,
    ))
    _set_session_cookie(resp, full_token, max_age=SESSION_HOURS * 3600)
    return resp


# ── Audit log viewer (admins) ─────────────────────────────────────────────────

AUDIT_ACTIONS = [
    "login_ok", "login_fail", "login_locked", "logout",
    "mfa_ok", "mfa_fail", "mfa_enroll",
    "pw_change", "pw_reset_request", "pw_reset_complete",
    "account_setup", "invite_send",
    "user_activate", "user_deactivate",
    "invoice_create", "invoice_mark_paid",
    "doc_upload", "doc_download", "doc_delete",
]

AUDIT_PAGE_SIZE = 100


@pages_bp.route("/portal/admin/audit")
@admin_required
def admin_audit():
    company = g.user["company"]
    filter_action = (request.args.get("action") or "").strip()
    filter_email  = (request.args.get("email")  or "").strip().lower()
    filter_since  = (request.args.get("since")  or "").strip()
    before_id     = request.args.get("before", type=int)

    sql = (
        "SELECT id, ts, user_id, email, action, target, ip, ua, metadata "
        "FROM portal_audit WHERE company = %s"
    )
    params: list = [company]
    if filter_action and filter_action in AUDIT_ACTIONS:
        sql += " AND action = %s"
        params.append(filter_action)
    if filter_email:
        sql += " AND email = %s"
        params.append(filter_email)
    if filter_since:
        try:
            datetime.strptime(filter_since, "%Y-%m-%d")
            sql += " AND ts >= %s"
            params.append(filter_since)
        except ValueError:
            filter_since = ""
    if before_id:
        sql += " AND id < %s"
        params.append(before_id)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(AUDIT_PAGE_SIZE + 1)

    rows = portal_db.query_all(sql, tuple(params))
    has_more = len(rows) > AUDIT_PAGE_SIZE
    rows = rows[:AUDIT_PAGE_SIZE]

    next_qs = ""
    if has_more and rows:
        last_id = rows[-1]["id"]
        from urllib.parse import urlencode
        next_qs = urlencode({k: v for k, v in {
            "action": filter_action,
            "email": filter_email,
            "since": filter_since,
            "before": last_id,
        }.items() if v})

    return render_template(
        "portal_admin_audit.html",
        rows=rows,
        action_options=AUDIT_ACTIONS,
        filter_action=filter_action,
        filter_email=filter_email,
        filter_since=filter_since,
        has_more=has_more,
        next_qs=next_qs,
    )


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
    audit_log("invoice_mark_paid", target=f"invoice:{invoice_id}")
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

    if not _magic_ok(f, ext):
        flash("File content does not match its extension.", "error")
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
    portal_storage.save(company, stored_name, f)

    mime = f.content_type or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
    portal_db.execute(
        "INSERT INTO portal_documents "
        "(user_id, company, filename, original_name, mime_type, size_bytes, uploaded_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (target_uid, company, stored_name, secure_filename(f.filename), mime, size, uid),
    )
    audit_log(
        "doc_upload",
        target=f"user:{target_uid}",
        metadata={"original_name": secure_filename(f.filename), "size": size, "mime": mime},
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

    audit_log(
        "doc_download",
        target=f"doc:{doc_id}",
        metadata={"original_name": doc["original_name"]},
    )
    return portal_storage.download(company, doc["filename"], doc["original_name"])


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

    portal_storage.delete(company, doc["filename"])

    portal_db.execute("DELETE FROM portal_documents WHERE id = %s", (doc_id,))
    audit_log(
        "doc_delete",
        target=f"doc:{doc_id}",
        metadata={"original_name": doc["original_name"], "user_id": doc["user_id"]},
    )
    flash("File deleted.", "success")
    return redirect("/portal/documents")
