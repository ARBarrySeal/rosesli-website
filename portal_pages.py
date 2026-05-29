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
                "JOIN portal_users u ON u.id = i.user_id "
                "WHERE u.company = %s AND u.role = 'employee'",
                (company,),
            )["n"],
            "client_invoices": portal_db.query_one(
                "SELECT COUNT(*) AS n FROM client_invoices ci "
                "JOIN portal_users u ON u.id = ci.client_id "
                "WHERE ci.company = %s AND u.role = 'client'",
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
    uid          = g.user["sub"]
    is_admin_view = False
    target_uid   = uid

    # Admins can view/edit another user's profile via ?uid=<id>
    if g.user["role"] == "admin":
        raw = (request.args.get("uid") or request.form.get("view_uid") or "").strip()
        if raw and raw != str(uid):
            try:
                candidate = int(raw)
                target = portal_db.query_one(
                    "SELECT id FROM portal_users WHERE id=%s AND company=%s AND role!='admin'",
                    (candidate, g.user["company"]),
                )
                if target:
                    target_uid    = candidate
                    is_admin_view = True
            except ValueError:
                pass

    user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))
    error   = None
    success = None

    if request.method == "POST":
        action = request.form.get("action")

        if action == "change_role" and is_admin_view:
            new_role = request.form.get("role")
            if new_role in ("employee", "client"):
                portal_db.execute(
                    "UPDATE portal_users SET role=%s WHERE id=%s",
                    (new_role, target_uid),
                )
                audit_log("role_change", user_id=uid, email=g.user["email"],
                          company=g.user["company"],
                          detail=f"uid={target_uid} role->{new_role}")
                user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))
                success = f"Role updated to {'Interpreter' if new_role == 'employee' else 'Client'}."

        elif action == "update":
            full_name    = (request.form.get("full_name")    or "").strip()[:255]
            phone        = (request.form.get("phone")        or "").strip()[:50]
            company_name = (request.form.get("company_name") or "").strip()[:255]
            address      = (request.form.get("address")      or "").strip()[:500]
            zip_code     = (request.form.get("zip")          or "").strip()[:20]
            role         = user["role"]
            if not full_name:
                error = "Full name is required."
            elif role == "employee":
                certification = (request.form.get("certification") or "").strip()[:255]
                rate_raw      = (request.form.get("interpreter_rate") or "").strip()
                try:
                    interpreter_rate = float(rate_raw) if rate_raw else None
                except ValueError:
                    interpreter_rate = None
                portal_db.execute(
                    "UPDATE portal_users SET full_name=%s, phone=%s, company_name=%s, "
                    "address=%s, zip=%s, certification=%s, interpreter_rate=%s WHERE id=%s",
                    (full_name, phone, company_name, address, zip_code or None,
                     certification or None, interpreter_rate, target_uid),
                )
                user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))
                success = "Profile updated."
            elif role == "client":
                poc           = (request.form.get("point_of_contact") or "").strip()[:255]
                billing_email = (request.form.get("billing_email") or "").strip()[:255]
                billing_phone = (request.form.get("billing_phone") or "").strip()[:50]
                rate_raw      = (request.form.get("interpreter_rate") or "").strip()
                try:
                    interpreter_rate = float(rate_raw) if rate_raw else None
                except ValueError:
                    interpreter_rate = None
                portal_db.execute(
                    "UPDATE portal_users SET full_name=%s, phone=%s, company_name=%s, "
                    "address=%s, zip=%s, point_of_contact=%s, billing_email=%s, "
                    "billing_phone=%s, interpreter_rate=%s WHERE id=%s",
                    (full_name, phone, company_name, address, zip_code or None,
                     poc or None, billing_email or None, billing_phone or None,
                     interpreter_rate, target_uid),
                )
                user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))
                success = "Profile updated."
            else:
                portal_db.execute(
                    "UPDATE portal_users SET full_name=%s, phone=%s, company_name=%s, "
                    "address=%s, zip=%s WHERE id=%s",
                    (full_name, phone, company_name, address, zip_code or None, target_uid),
                )
                user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))
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
                    is_admin_view=False,
                    view_uid=None,
                    w9_attachment=_get_w9(target_uid),
                ))
                _set_session_cookie(resp, fresh_token, max_age=SESSION_HOURS * 3600)
                return resp

    return render_template(
        "portal_profile.html",
        user=user,
        error=error,
        success=success,
        is_admin_view=is_admin_view,
        view_uid=target_uid if is_admin_view else None,
        w9_attachment=_get_w9(target_uid),
    )


def _get_w9(user_id: int):
    try:
        return portal_db.query_one(
            "SELECT id, original_name, gcs_key FROM profile_w9 WHERE user_id = %s "
            "ORDER BY uploaded_at DESC LIMIT 1",
            (user_id,),
        )
    except Exception:
        return None


@pages_bp.route("/portal/profile/w9/upload", methods=["POST"])
@login_required
def w9_upload():
    uid     = g.user["sub"]
    company = g.user["company"]

    raw = (request.form.get("view_uid") or "").strip()
    if raw and g.user["role"] == "admin":
        try:
            target_uid = int(raw)
        except ValueError:
            target_uid = uid
    else:
        target_uid = uid

    f = request.files.get("w9_file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        redir = f"/portal/profile?uid={target_uid}" if target_uid != uid else "/portal/profile"
        return redirect(redir)

    _, ext = os.path.splitext(secure_filename(f.filename))
    ext = ext.lower()
    if ext not in ALLOWED_EXTENSIONS:
        flash("File type not allowed.", "error")
        redir = f"/portal/profile?uid={target_uid}" if target_uid != uid else "/portal/profile"
        return redirect(redir)

    f.stream.seek(0, 2)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        flash("File too large (max 25 MB).", "error")
        redir = f"/portal/profile?uid={target_uid}" if target_uid != uid else "/portal/profile"
        return redirect(redir)

    stored_name = uuid.uuid4().hex + ext
    portal_storage.save(company, stored_name, f.stream)

    try:
        portal_db.execute(
            "DELETE FROM profile_w9 WHERE user_id = %s AND company = %s",
            (target_uid, company),
        )
        portal_db.execute(
            "INSERT INTO profile_w9 (user_id, company, year, gcs_key, original_name, uploaded_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW())",
            (target_uid, company, datetime.now(timezone.utc).year,
             stored_name, secure_filename(f.filename)),
        )
    except Exception:
        flash("Could not save W-9 (database not ready).", "error")
        redir = f"/portal/profile?uid={target_uid}" if target_uid != uid else "/portal/profile"
        return redirect(redir)

    audit_log("w9_upload", user_id=uid, email=g.user["email"], company=company,
              detail=f"target_uid={target_uid}")
    flash("W-9 uploaded.", "success")
    redir = f"/portal/profile?uid={target_uid}" if target_uid != uid else "/portal/profile"
    return redirect(redir)


@pages_bp.route("/portal/profile/w9/download")
@login_required
def w9_download():
    uid     = g.user["sub"]
    company = g.user["company"]

    raw = (request.args.get("uid") or "").strip()
    if raw and g.user["role"] == "admin":
        try:
            target_uid = int(raw)
        except ValueError:
            target_uid = uid
    else:
        target_uid = uid

    try:
        row = portal_db.query_one(
            "SELECT gcs_key, original_name FROM profile_w9 "
            "WHERE user_id = %s AND company = %s ORDER BY uploaded_at DESC LIMIT 1",
            (target_uid, company),
        )
    except Exception:
        abort(404)

    if not row:
        abort(404)
    return portal_storage.download(company, row["gcs_key"], row["original_name"])


@pages_bp.route("/portal/profile/w9/delete", methods=["POST"])
@login_required
def w9_delete():
    uid     = g.user["sub"]
    company = g.user["company"]

    if g.user["role"] != "admin":
        abort(403)

    raw = (request.form.get("view_uid") or "").strip()
    try:
        target_uid = int(raw) if raw else uid
    except ValueError:
        target_uid = uid

    try:
        row = portal_db.query_one(
            "SELECT gcs_key FROM profile_w9 WHERE user_id = %s AND company = %s",
            (target_uid, company),
        )
        if row:
            portal_storage.delete(company, row["gcs_key"])
            portal_db.execute(
                "DELETE FROM profile_w9 WHERE user_id = %s AND company = %s",
                (target_uid, company),
            )
    except Exception:
        pass

    audit_log("w9_delete", user_id=uid, email=g.user["email"], company=company,
              detail=f"target_uid={target_uid}")
    flash("W-9 removed.", "success")
    return redirect(f"/portal/profile?uid={target_uid}")


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
        "FROM portal_users WHERE company = %s AND (archived IS NULL OR archived = FALSE) "
        "ORDER BY created_at DESC",
        (company,),
    )
    return render_template("portal_admin_users.html", users=users)


@pages_bp.route("/portal/admin/users/<int:user_id>/archive", methods=["POST"])
@admin_required
def archive_user(user_id):
    company = g.user["company"]
    user    = portal_db.query_one(
        "SELECT id, email, role FROM portal_users WHERE id = %s AND company = %s",
        (user_id, company),
    )
    if not user:
        return jsonify(ok=False, error="User not found"), 404
    if user["role"] == "admin":
        return jsonify(ok=False, error="Cannot archive admin accounts"), 400
    portal_db.execute(
        "UPDATE portal_users SET active = FALSE, archived = TRUE, archived_at = NOW() "
        "WHERE id = %s",
        (user_id,),
    )
    audit_log("user_archive", target=f"user:{user_id}", metadata={"email": user["email"]})
    return jsonify(ok=True)


@pages_bp.route("/portal/admin/archived-users")
@admin_required
def archived_users():
    company = g.user["company"]
    users   = portal_db.query_all(
        "SELECT id, email, full_name, role, archived_at "
        "FROM portal_users WHERE company = %s AND archived = TRUE "
        "ORDER BY archived_at DESC",
        (company,),
    )
    return render_template("portal_archived_users.html", users=users)


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
            "SELECT i.id, u.full_name, u.email, u.role AS user_role, i.amount, i.status, "
            "i.due_date, i.created_at, i.submitted "
            "FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "WHERE u.company = %s ORDER BY i.created_at DESC",
            (company,),
        )
        interpreters = portal_db.query_all(
            "SELECT id, full_name, email FROM portal_users "
            "WHERE company = %s AND role = 'employee' AND active = TRUE ORDER BY full_name",
            (company,),
        )
    else:
        rows = portal_db.query_all(
            "SELECT id, amount, status, due_date, created_at, submitted "
            "FROM invoices WHERE user_id = %s ORDER BY created_at DESC",
            (uid,),
        )
        interpreters = []
    return render_template("portal_invoices.html", invoices=rows, interpreters=interpreters)


@pages_bp.route("/portal/unpaid-invoices")
@login_required
def unpaid_invoices():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        rows = portal_db.query_all(
            "SELECT i.id, u.full_name, u.email, i.amount, i.due_date, i.created_at "
            "FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "WHERE u.company = %s AND i.status = 'unpaid' ORDER BY i.created_at DESC",
            (company,),
        )
    else:
        rows = portal_db.query_all(
            "SELECT id, amount, due_date, created_at FROM invoices "
            "WHERE user_id = %s AND status = 'unpaid' ORDER BY created_at DESC",
            (uid,),
        )
    return render_template("portal_unpaid_invoices.html", invoices=rows)


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
    attachments = portal_db.query_all(
        "SELECT id, original_name, size_bytes, created_at FROM invoice_attachments "
        "WHERE invoice_id = %s ORDER BY created_at",
        (invoice_id,),
    )
    return render_template("portal_invoice.html", inv=inv, attachments=attachments)


@pages_bp.route("/portal/invoices/<int:invoice_id>/attachments", methods=["POST"])
@login_required
def upload_invoice_attachment(invoice_id):
    """Admin attaches files or photos to an invoice. Mirrors document upload."""
    uid     = g.user["sub"]
    company = g.user["company"]
    if g.user["role"] != "admin":
        abort(403)

    inv = portal_db.query_one(
        "SELECT i.id FROM invoices i JOIN portal_users u ON u.id = i.user_id "
        "WHERE i.id = %s AND u.company = %s",
        (invoice_id, company),
    )
    if not inv:
        abort(404)

    if "file" not in request.files or not request.files["file"].filename:
        flash("No file selected.", "error")
        return redirect(f"/portal/invoices/{invoice_id}")

    f = request.files["file"]
    _, ext = os.path.splitext(secure_filename(f.filename))
    if ext.lower() not in ALLOWED_EXTENSIONS:
        flash("File type not allowed.", "error")
        return redirect(f"/portal/invoices/{invoice_id}")

    f.seek(0, os.SEEK_END)
    size = f.tell()
    f.seek(0)
    if size > MAX_UPLOAD_BYTES:
        flash("File too large (25 MB max).", "error")
        return redirect(f"/portal/invoices/{invoice_id}")

    stored_name = uuid.uuid4().hex + ext.lower()
    portal_storage.save(company, stored_name, f)
    mime = f.content_type or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
    portal_db.execute(
        "INSERT INTO invoice_attachments "
        "(invoice_id, company, filename, original_name, mime_type, size_bytes, uploaded_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (invoice_id, company, stored_name, secure_filename(f.filename), mime, size, uid),
    )
    audit_log(
        "invoice_attach",
        target=f"invoice:{invoice_id}",
        metadata={"original_name": secure_filename(f.filename), "size": size, "mime": mime},
    )
    flash("Attachment uploaded.", "success")
    return redirect(f"/portal/invoices/{invoice_id}")


@pages_bp.route("/portal/invoices/<int:invoice_id>/attachments/<int:att_id>/download")
@login_required
def download_invoice_attachment(invoice_id, att_id):
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        inv = portal_db.query_one(
            "SELECT i.id FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "WHERE i.id = %s AND u.company = %s",
            (invoice_id, company),
        )
    else:
        inv = portal_db.query_one(
            "SELECT id FROM invoices WHERE id = %s AND user_id = %s",
            (invoice_id, uid),
        )
    if not inv:
        abort(404)

    att = portal_db.query_one(
        "SELECT * FROM invoice_attachments WHERE id = %s AND invoice_id = %s",
        (att_id, invoice_id),
    )
    if not att:
        abort(404)
    audit_log(
        "invoice_attach_download",
        target=f"invoice:{invoice_id}",
        metadata={"original_name": att["original_name"]},
    )
    return portal_storage.download(company, att["filename"], att["original_name"])


@pages_bp.route("/portal/invoices/<int:invoice_id>/attachments/<int:att_id>/delete", methods=["POST"])
@login_required
def delete_invoice_attachment(invoice_id, att_id):
    company = g.user["company"]
    if g.user["role"] != "admin":
        abort(403)

    inv = portal_db.query_one(
        "SELECT i.id FROM invoices i JOIN portal_users u ON u.id = i.user_id "
        "WHERE i.id = %s AND u.company = %s",
        (invoice_id, company),
    )
    if not inv:
        abort(404)

    att = portal_db.query_one(
        "SELECT * FROM invoice_attachments WHERE id = %s AND invoice_id = %s",
        (att_id, invoice_id),
    )
    if not att:
        abort(404)

    portal_storage.delete(company, att["filename"])
    portal_db.execute("DELETE FROM invoice_attachments WHERE id = %s", (att_id,))
    audit_log(
        "invoice_attach_delete",
        target=f"invoice:{invoice_id}",
        metadata={"original_name": att["original_name"]},
    )
    flash("Attachment deleted.", "success")
    return redirect(f"/portal/invoices/{invoice_id}")


@pages_bp.route("/portal/invoices/<int:invoice_id>/submit", methods=["POST"])
@login_required
def submit_invoice(invoice_id):
    """Interpreter submits their invoice to admin for approval."""
    uid  = g.user["sub"]
    role = g.user["role"]
    if role == "client":
        abort(403)
    if role == "employee":
        inv = portal_db.query_one(
            "SELECT id FROM invoices WHERE id = %s AND user_id = %s",
            (invoice_id, uid),
        )
    else:
        company = g.user["company"]
        inv = portal_db.query_one(
            "SELECT i.id FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "WHERE i.id = %s AND u.company = %s",
            (invoice_id, company),
        )
    if not inv:
        abort(404)
    portal_db.execute(
        "UPDATE invoices SET submitted = TRUE, submitted_at = NOW() WHERE id = %s",
        (invoice_id,),
    )
    flash("Invoice submitted for approval.", "success")
    return redirect(f"/portal/invoices/{invoice_id}")


@pages_bp.route("/portal/admin/invoices/<int:invoice_id>/mark-unpaid", methods=["POST"])
@admin_required
def mark_invoice_unpaid(invoice_id):
    company = g.user["company"]
    inv = portal_db.query_one(
        "SELECT i.id FROM invoices i JOIN portal_users u ON u.id = i.user_id "
        "WHERE i.id = %s AND u.company = %s",
        (invoice_id, company),
    )
    if not inv:
        abort(404)
    portal_db.execute(
        "UPDATE invoices SET status='unpaid', paid_date=NULL WHERE id=%s", (invoice_id,)
    )
    audit_log("invoice_mark_unpaid", target=f"invoice:{invoice_id}")
    flash("Invoice marked as unpaid.", "success")
    return redirect(f"/portal/invoices/{invoice_id}")


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
    paid_date = request.form.get("paid_date") or None
    portal_db.execute(
        "UPDATE invoices SET status='paid', paid_date=%s WHERE id=%s", (paid_date, invoice_id)
    )
    audit_log("invoice_mark_paid", target=f"invoice:{invoice_id}", metadata={"paid_date": paid_date})
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
