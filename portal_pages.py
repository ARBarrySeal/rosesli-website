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
import portal_email
import portal_mfa
import portal_storage
from portal_audit import log as audit_log
from portal_auth import (
    COMPANY_NAMES, SESSION_HOURS, _password_too_weak, _set_session_cookie,
    admin_required, check_password, decode_jwt, encode_jwt,
    generate_temp_password, hash_password, login_required, rotate_csrf_token,
)

# All 50 states, alphabetical by code — shared by profile and assignment forms
# (exposed to templates as the `us_states` Jinja global in main.py).
US_STATES = [
    "AK", "AL", "AR", "AZ", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD",
    "ME", "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE", "NH",
    "NJ", "NM", "NV", "NY", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY",
]

# Rose SLI profile dropdowns. Columns stay TEXT — off-list values already in
# the DB render as an extra selected option so nothing is lost.
CERT_OPTIONS = ["RID", "NAD Master", "NIC Master", "CDI", "Other"]
SPECIALTY_OPTIONS = ["K-12", "Higher Ed", "Conference", "Performance",
                     "Deafblind", "Government", "Other"]

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


def _upload_error(f) -> str | None:
    """Validate one uploaded file (extension allowlist, magic bytes, size).
    Returns a user-facing error string, or None when the file is acceptable."""
    if not f or not f.filename:
        return "No file selected."
    _, ext = os.path.splitext(secure_filename(f.filename))
    if ext.lower() not in ALLOWED_EXTENSIONS:
        return "File type not allowed."
    if not _magic_ok(f, ext):
        return "File content does not match its extension."
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > MAX_UPLOAD_BYTES:
        return "File too large (25 MB max)."
    return None

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
    uid     = int(g.user["sub"])
    from datetime import date
    today = date.today()

    # ── Admin dashboard ───────────────────────────────────────────────────────
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
        return render_template("portal_dashboard_admin.html", stats=stats, invoices=invoices)

    # ── Interpreter (employee) dashboard ──────────────────────────────────────
    if role == "employee":
        upcoming = portal_db.query_all(
            "SELECT id, job_number, event_date, start_time, end_time, setting, "
            "       event_address, status, client_name "
            "FROM jobs WHERE company = %s AND (interpreter_1_id = %s OR interpreter_2_id = %s "
            "     OR EXISTS (SELECT 1 FROM job_interpreters ji "
            "                WHERE ji.job_id = jobs.id AND ji.interpreter_id = %s)) "
            "AND event_date >= %s AND status IN ('confirmed', 'completed') "
            "ORDER BY event_date, start_time LIMIT 6",
            (company, uid, uid, uid, today),
        )
        offers = portal_db.query_all(
            "SELECT o.id, j.id AS job_id, j.job_number, j.event_date, j.start_time, "
            "       j.end_time, j.setting, j.event_address "
            "FROM job_offers o JOIN jobs j ON j.id = o.job_id "
            "WHERE o.interpreter_id = %s AND o.company = %s AND o.status = 'offered' "
            "ORDER BY j.event_date NULLS LAST, o.offered_at LIMIT 6",
            (uid, company),
        )
        inv = portal_db.query_one(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(CASE WHEN status = 'unpaid' THEN 1 ELSE 0 END), 0) AS unpaid "
            "FROM invoices WHERE user_id = %s",
            (uid,),
        )
        return render_template(
            "portal_dashboard_interpreter.html",
            upcoming=upcoming, offers=offers,
            invoice_count=inv["n"], unpaid_count=inv["unpaid"],
        )

    # ── Client dashboard ──────────────────────────────────────────────────────
    if role == "client":
        requests_ = portal_db.query_all(
            "SELECT id, job_number, event_date, start_time, setting, event_address, status "
            "FROM jobs WHERE company = %s AND client_id = %s AND status = 'pending' "
            "ORDER BY event_date NULLS LAST, created_at DESC LIMIT 6",
            (company, uid),
        )
        upcoming = portal_db.query_all(
            "SELECT id, job_number, event_date, start_time, end_time, setting, event_address, status "
            "FROM jobs WHERE company = %s AND client_id = %s AND status = 'confirmed' "
            "AND event_date >= %s ORDER BY event_date, start_time LIMIT 6",
            (company, uid, today),
        )
        invoices = portal_db.query_all(
            "SELECT id, date_of_service, total, status FROM client_invoices "
            "WHERE company = %s AND client_id = %s "
            "ORDER BY date_of_service DESC NULLS LAST, id DESC LIMIT 6",
            (company, uid),
        )
        unpaid = portal_db.query_one(
            "SELECT COUNT(*) AS n FROM client_invoices "
            "WHERE company = %s AND client_id = %s AND status = 'unpaid'",
            (company, uid),
        )["n"]
        return render_template(
            "portal_dashboard_client.html",
            requests=requests_, upcoming=upcoming, invoices=invoices, unpaid_count=unpaid,
        )

    abort(403)


def _compose_full_name(first, mi, last):
    """'First M. Last' — full_name stays authoritative for the dozens of
    existing call sites (emails, job snapshots, offers, autocomplete)."""
    mid = f"{mi}." if mi else ""
    return " ".join(p for p in (first, mid, last) if p)


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

    user      = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))
    documents = _get_user_documents(target_uid)
    error     = None
    success   = None

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
                          target=f"user:{target_uid}",
                          metadata={"new_role": new_role})
                user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))
                success = f"Role updated to {'Interpreter' if new_role == 'employee' else 'Client'}."

        elif action == "set_temp_password" and is_admin_view:
            # Admin-issued temporary password: emailed to the user and shown
            # once here; the user must replace it at next sign-in.
            temp_pw = generate_temp_password()
            portal_db.execute(
                "UPDATE portal_users "
                "SET password_hash = %s, must_change_password = TRUE, "
                "    pw_changed_at = NOW(), failed_login_count = 0, locked_until = NULL, "
                "    reset_token = NULL, reset_expires = NULL "
                "WHERE id = %s",
                (hash_password(temp_pw), target_uid),
            )
            audit_log("pw_temp_issued", user_id=uid, email=g.user["email"],
                      company=g.user["company"],
                      target=f"user:{target_uid}",
                      metadata={"via": "admin_profile"})
            app_url = os.environ.get("APP_URL", "http://localhost:8080")
            from portal_email import send_temp_password_email
            emailed = send_temp_password_email(
                user["email"], temp_pw, f"{app_url}/login",
                COMPANY_NAMES.get(g.user["company"], g.user["company"]),
            )
            success = (
                f"Temporary password issued: {temp_pw} — "
                + ("it was also emailed to the user. " if emailed
                   else "email could not be sent, so share it with the user directly. ")
                + "They'll be asked to create a new password at next sign-in."
            )

        elif action == "set_rate" and is_admin_view and user["company"] == "rosesli":
            import portal_rates
            from datetime import date as _date
            rate_raw = (request.form.get("new_rate") or "").strip()
            eff_raw  = (request.form.get("effective_date") or "").strip()
            try:
                new_rate = float(rate_raw)
                if new_rate < 0:
                    raise ValueError
            except ValueError:
                new_rate = None
            try:
                eff_date = _date.fromisoformat(eff_raw) if eff_raw else _date.today()
            except ValueError:
                eff_date = None
            if new_rate is None:
                error = "Rate must be a non-negative number."
            elif eff_date is None:
                error = "Effective date must be a valid date (YYYY-MM-DD)."
            else:
                summary = portal_rates.set_rate(
                    user["company"], target_uid, new_rate, eff_date,
                    created_by=int(g.user["sub"]),
                )
                audit_log(
                    "rate_change",
                    target=f"user:{target_uid}",
                    metadata={"rate": new_rate, "effective_date": str(eff_date),
                              "recalc": summary},
                )
                recalced = summary["jobs"] + summary["client_invoices"] + summary["interpreter_invoices"]
                success = (
                    f"Rate ${new_rate:.2f}/hr effective {eff_date} saved."
                    + (f" {recalced} open record(s) with service date on/after "
                       f"{eff_date} were recalculated." if recalced else "")
                )
                user = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))

        elif action == "update":
            rosesli      = user["company"] == "rosesli"
            phone        = (request.form.get("phone")        or "").strip()[:50]
            company_name = (request.form.get("company_name") or "").strip()[:255]
            address      = (request.form.get("address")      or "").strip()[:500]
            zip_code     = (request.form.get("zip")          or "").strip()[:20]
            role         = user["role"]

            if rosesli:
                first = (request.form.get("first_name")     or "").strip()[:100]
                last  = (request.form.get("last_name")      or "").strip()[:100]
                mi    = (request.form.get("middle_initial") or "").strip().rstrip(".")[:1].upper()
                street = (request.form.get("address_street") or "").strip()[:255]
                city   = (request.form.get("address_city")   or "").strip()[:100]
                state  = (request.form.get("address_state")  or "").strip().upper()[:2]
                if state not in US_STATES:
                    state = ""
                full_name = _compose_full_name(first, mi, last)
            else:
                full_name = (request.form.get("full_name") or "").strip()[:255]

            # Base rate is admin-set only — a non-admin POST silently keeps the
            # stored value (closes the old unrestricted self-edit).
            rate_raw = (request.form.get("interpreter_rate") or "").strip()
            try:
                interpreter_rate = float(rate_raw) if rate_raw else None
            except ValueError:
                interpreter_rate = None
            rate_sql, rate_vals = ("", [])
            if is_admin_view and not rosesli:
                # rosesli rate changes go through the effective-dated Rate
                # History form (action=set_rate) instead of a direct write.
                rate_sql, rate_vals = (", interpreter_rate=%s", [interpreter_rate])

            name_sql, name_vals = ("full_name=%s", [full_name])
            if rosesli:
                name_sql = ("full_name=%s, first_name=%s, last_name=%s, middle_initial=%s, "
                            "address_street=%s, address_city=%s, address_state=%s")
                name_vals = [full_name, first or None, last or None, mi or None,
                             street or None, city or None, state or None]

            if not full_name:
                error = ("First name is required." if rosesli else "Full name is required.")
            elif role == "employee":
                certification = (request.form.get("certification") or "").strip()[:255]
                specialty     = (request.form.get("specialty")     or "").strip()[:255]
                # rosesli interpreter form has no Company/Organization or free-text
                # address fields — leave those columns untouched.
                extra_sql  = ", zip=%s, certification=%s, specialty=%s"
                extra_vals = [zip_code or None, certification or None, specialty or None]
                if not rosesli:
                    extra_sql  = ", company_name=%s, address=%s" + extra_sql
                    extra_vals = [company_name, address] + extra_vals
                portal_db.execute(
                    f"UPDATE portal_users SET {name_sql}, phone=%s{extra_sql}{rate_sql} WHERE id=%s",
                    tuple(name_vals + [phone] + extra_vals + rate_vals + [target_uid]),
                )
                user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))
                success = "Profile updated."
            elif role == "client":
                poc           = (request.form.get("point_of_contact") or "").strip()[:255]
                billing_email = (request.form.get("billing_email") or "").strip()[:255]
                billing_phone = (request.form.get("billing_phone") or "").strip()[:50]
                extra_sql  = ", zip=%s, point_of_contact=%s, billing_email=%s, billing_phone=%s"
                extra_vals = [zip_code or None, poc or None, billing_email or None,
                              billing_phone or None]
                if not rosesli:
                    extra_sql  = ", address=%s" + extra_sql
                    extra_vals = [address] + extra_vals
                portal_db.execute(
                    f"UPDATE portal_users SET {name_sql}, phone=%s, company_name=%s"
                    f"{extra_sql}{rate_sql} WHERE id=%s",
                    tuple(name_vals + [phone, company_name] + extra_vals + rate_vals + [target_uid]),
                )
                user    = portal_db.query_one("SELECT * FROM portal_users WHERE id = %s", (target_uid,))
                success = "Profile updated."
            else:
                # rosesli forms have no free-text address field — leave the
                # legacy column untouched (split fields are in name_sql).
                extra_sql  = ", zip=%s" if rosesli else ", address=%s, zip=%s"
                extra_vals = ([zip_code or None] if rosesli
                              else [address, zip_code or None])
                portal_db.execute(
                    f"UPDATE portal_users SET {name_sql}, phone=%s, company_name=%s"
                    f"{extra_sql} WHERE id=%s",
                    tuple(name_vals + [phone, company_name] + extra_vals + [target_uid]),
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
                    documents=documents,
                ))
                _set_session_cookie(resp, fresh_token, max_age=SESSION_HOURS * 3600)
                return resp

    rate_history = None
    if is_admin_view and user["company"] == "rosesli" and user["role"] in ("employee", "client"):
        import portal_rates
        rate_history = portal_rates.rate_history_for(target_uid)

    return render_template(
        "portal_profile.html",
        user=user,
        error=error,
        success=success,
        is_admin_view=is_admin_view,
        view_uid=target_uid if is_admin_view else None,
        w9_attachment=_get_w9(target_uid),
        documents=documents,
        rate_history=rate_history,
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


def _get_user_documents(user_id: int):
    """Files attached to a user's profile (reuses the portal_documents store)."""
    try:
        return portal_db.query_all(
            "SELECT id, original_name, size_bytes, created_at FROM portal_documents "
            "WHERE user_id = %s AND job_id IS NULL ORDER BY created_at DESC",
            (user_id,),
        )
    except Exception:
        return []


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
              target=f"user:{target_uid}")
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
              target=f"user:{target_uid}")
    flash("W-9 removed.", "success")
    return redirect(f"/portal/profile?uid={target_uid}")


@pages_bp.route("/portal/admin/users")
@admin_required
def admin_users():
    company = g.user["company"]
    users   = portal_db.query_all(
        "SELECT id, email, full_name, first_name, last_name, role, active, created_at "
        "FROM portal_users WHERE company = %s AND (archived IS NULL OR archived = FALSE) "
        "ORDER BY created_at DESC",
        (company,),
    )
    return render_template("portal_admin_users.html", users=users)


@pages_bp.route("/portal/admin/interpreters")
@admin_required
def admin_interpreters():
    company = g.user["company"]
    users   = portal_db.query_all(
        "SELECT id, email, full_name, first_name, last_name, certification, "
        "       interpreter_rate, active, created_at "
        "FROM portal_users WHERE company = %s AND role = 'employee' "
        "AND (archived IS NULL OR archived = FALSE) "
        "ORDER BY full_name NULLS LAST, created_at DESC",
        (company,),
    )
    return render_template("portal_admin_interpreters.html", users=users)


@pages_bp.route("/portal/admin/clients")
@admin_required
def admin_clients():
    company = g.user["company"]
    users   = portal_db.query_all(
        "SELECT id, email, full_name, first_name, last_name, company_name, "
        "       point_of_contact, interpreter_rate, active, created_at "
        "FROM portal_users WHERE company = %s AND role = 'client' "
        "AND (archived IS NULL OR archived = FALSE) "
        "ORDER BY full_name NULLS LAST, created_at DESC",
        (company,),
    )
    return render_template("portal_admin_clients.html", users=users)


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
            "i.due_date, i.created_at, i.submitted, j.job_number "
            "FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "LEFT JOIN jobs j ON j.id = i.job_id "
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
            "SELECT i.id, i.amount, i.status, i.due_date, i.created_at, i.submitted, j.job_number "
            "FROM invoices i LEFT JOIN jobs j ON j.id = i.job_id "
            "WHERE i.user_id = %s ORDER BY i.created_at DESC",
            (uid,),
        )
        interpreters = []
    if role == "employee":
        open_invoices = [r for r in rows if not r["submitted"]]
        submitted_invoices = [r for r in rows if r["submitted"]]
        return render_template("portal_invoices.html", invoices=rows, interpreters=interpreters,
                               open_invoices=open_invoices, submitted_invoices=submitted_invoices)
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
            "SELECT i.*, u.full_name, u.email, j.job_number FROM invoices i "
            "JOIN portal_users u ON u.id = i.user_id "
            "LEFT JOIN jobs j ON j.id = i.job_id "
            "WHERE i.id = %s AND u.company = %s",
            (invoice_id, company),
        )
    else:
        inv = portal_db.query_one(
            "SELECT i.*, j.job_number FROM invoices i "
            "LEFT JOIN jobs j ON j.id = i.job_id "
            "WHERE i.id = %s AND i.user_id = %s",
            (invoice_id, uid),
        )

    if not inv:
        abort(404)
    try:
        expenses = json.loads(inv["expenses"]) if inv.get("expenses") else []
    except (TypeError, ValueError):
        expenses = []
    attachments = portal_db.query_all(
        "SELECT id, original_name, size_bytes, created_at FROM invoice_attachments "
        "WHERE invoice_id = %s ORDER BY created_at",
        (invoice_id,),
    )
    # Master invoices (Phase 6) bundle multiple assignments; show the breakdown.
    # LEFT JOIN so a since-deleted assignment still shows its recorded amount.
    lines = portal_db.query_all(
        "SELECT ij.line_amount, j.event_date, j.setting, j.job_number "
        "FROM invoice_jobs ij LEFT JOIN jobs j ON j.id = ij.job_id "
        "WHERE ij.invoice_id = %s ORDER BY j.event_date NULLS LAST, ij.id",
        (invoice_id,),
    )
    return render_template("portal_invoice.html", inv=inv, attachments=attachments,
                           lines=lines, expenses=expenses)


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


def _notify_invoice_submitted(company, invoice_id, amount):
    """Email coordinators that an individual invoice was locked + submitted
    for review (best-effort — must never block the submit itself)."""
    try:
        interpreter_name = g.user.get("name") or g.user.get("email") or "An interpreter"
        link = request.url_root.rstrip("/") + "/portal/admin/interpreter-review"
        company_name = COMPANY_NAMES.get(company, company)
        for email in portal_email.coordinator_recipients(company):
            portal_email.send_invoice_submitted_email(
                email, interpreter_name, invoice_id, amount, link, company_name)
    except Exception:
        pass


@pages_bp.route("/portal/invoices/<int:invoice_id>/submit", methods=["POST"])
@login_required
def submit_invoice(invoice_id):
    """Interpreter submits their invoice to admin for approval; locks it from
    further interpreter edits and emails the coordinators (Interpreter Review)."""
    uid     = g.user["sub"]
    role    = g.user["role"]
    company = g.user["company"]
    if role == "client":
        abort(403)
    if role == "employee":
        inv = portal_db.query_one(
            "SELECT id, amount FROM invoices WHERE id = %s AND user_id = %s",
            (invoice_id, uid),
        )
    else:
        inv = portal_db.query_one(
            "SELECT i.id, i.amount FROM invoices i JOIN portal_users u ON u.id = i.user_id "
            "WHERE i.id = %s AND u.company = %s",
            (invoice_id, company),
        )
    if not inv:
        abort(404)
    portal_db.execute(
        "UPDATE invoices SET submitted = TRUE, submitted_at = NOW() WHERE id = %s",
        (invoice_id,),
    )
    if role == "employee":
        _notify_invoice_submitted(company, invoice_id, float(inv["amount"]))
    flash("Invoice submitted for review.", "success")
    return redirect(f"/portal/invoices/{invoice_id}")


@pages_bp.route("/portal/invoices/submit-batch", methods=["POST"])
@login_required
def submit_invoices_batch():
    """Interpreter selects multiple not-yet-submitted invoices from the
    Interpreter Invoices list and submits them all for review in one action."""
    uid     = g.user["sub"]
    role    = g.user["role"]
    company = g.user["company"]
    if role != "employee":
        abort(403)
    ids_raw = request.form.getlist("invoice_ids")
    try:
        ids = [int(i) for i in ids_raw]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        flash("Select at least one invoice to submit.", "error")
        return redirect("/portal/invoices")

    rows = portal_db.query_all(
        "SELECT id, amount FROM invoices "
        "WHERE id = ANY(%s) AND user_id = %s AND COALESCE(submitted, FALSE) = FALSE",
        (ids, uid),
    )
    if not rows:
        flash("Nothing eligible to submit.", "error")
        return redirect("/portal/invoices")

    submitted_ids = [r["id"] for r in rows]
    portal_db.execute(
        "UPDATE invoices SET submitted = TRUE, submitted_at = NOW() WHERE id = ANY(%s)",
        (submitted_ids,),
    )
    total = round(sum(float(r["amount"]) for r in rows), 2)
    _notify_invoice_submitted(company, submitted_ids[0], total)
    flash(f"{len(submitted_ids)} invoice(s) submitted for review.", "success")
    return redirect("/portal/invoices")


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
        "SELECT i.id, i.amount, u.email, u.full_name "
        "FROM invoices i JOIN portal_users u ON u.id = i.user_id "
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
    # Close the loop with the interpreter; a send failure must not block the action.
    if inv.get("email"):
        try:
            company_name = {
                "rosesli": "Rose Sign Language Interpreting",
                "dod": "DOD Cyber Consulting",
            }.get(company, company)
            link = request.url_root.rstrip("/") + f"/portal/invoices/{invoice_id}"
            portal_email.send_invoice_paid_email(
                inv["email"], inv.get("full_name") or "there", invoice_id,
                float(inv["amount"] or 0), link, company_name,
            )
        except Exception:
            pass
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
            "WHERE d.company = %s AND d.job_id IS NULL ORDER BY d.created_at DESC",
            (company,),
        )
        users = portal_db.query_all(
            "SELECT id, full_name, email FROM portal_users "
            "WHERE company = %s AND active = TRUE ORDER BY full_name",
            (company,),
        )
    else:
        docs = portal_db.query_all(
            "SELECT * FROM portal_documents WHERE user_id = %s AND job_id IS NULL ORDER BY created_at DESC",
            (uid,),
        )
        users = []
    return render_template("portal_documents.html", docs=docs, users=users)


def _doc_redirect():
    """Safe same-site redirect target for document actions (defaults to Documents)."""
    dest = (request.form.get("redirect_to") or request.args.get("redirect_to") or "").strip()
    return dest if dest.startswith("/portal/") else "/portal/documents"


@pages_bp.route("/portal/documents/upload", methods=["POST"])
@login_required
def upload_document():
    uid     = g.user["sub"]
    company = g.user["company"]
    role    = g.user["role"]
    dest    = _doc_redirect()

    if "file" not in request.files:
        flash("No file selected.", "error")
        return redirect(dest)

    f = request.files["file"]
    err = _upload_error(f)
    if err:
        flash(err, "error")
        return redirect(dest)
    _, ext = os.path.splitext(secure_filename(f.filename))
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)

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
    return redirect(dest)


@pages_bp.route("/portal/documents/<int:doc_id>/download")
@login_required
def download_document(doc_id):
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        doc = portal_db.query_one(
            "SELECT * FROM portal_documents WHERE id = %s AND company = %s AND job_id IS NULL",
            (doc_id, company),
        )
    else:
        doc = portal_db.query_one(
            "SELECT * FROM portal_documents WHERE id = %s AND user_id = %s AND job_id IS NULL",
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
    dest    = _doc_redirect()

    if role == "admin":
        doc = portal_db.query_one(
            "SELECT * FROM portal_documents WHERE id = %s AND company = %s AND job_id IS NULL",
            (doc_id, company),
        )
    else:
        doc = portal_db.query_one(
            "SELECT * FROM portal_documents WHERE id = %s AND user_id = %s AND job_id IS NULL",
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
    return redirect(dest)


# ── Assignment documents (job-scoped rows in portal_documents) ────────────────
# Prep materials attached to a job. Admin uploads/deletes; download is limited
# to admins and interpreters actually staffed on the job (join table + legacy
# slots via is_staffed_on). These rows have job_id set, so they never appear
# in the user-documents pages above (all filtered job_id IS NULL).

def _job_or_404(job_id, company):
    job = portal_db.query_one(
        "SELECT id FROM jobs WHERE id = %s AND company = %s", (job_id, company),
    )
    if not job:
        abort(404)
    return job


@pages_bp.route("/portal/admin/assignments/<int:job_id>/documents", methods=["POST"])
@admin_required
def upload_job_documents(job_id):
    company = g.user["company"]
    uid     = g.user["sub"]
    dest    = f"/portal/assignments/{job_id}"
    _job_or_404(job_id, company)

    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        flash("No file selected.", "error")
        return redirect(dest)

    saved = 0
    for f in files:
        err = _upload_error(f)
        if err:
            flash(f"{secure_filename(f.filename)}: {err}", "error")
            continue
        _, ext = os.path.splitext(secure_filename(f.filename))
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        stored_name = uuid.uuid4().hex + ext.lower()
        portal_storage.save(company, stored_name, f)
        mime = f.content_type or mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
        portal_db.execute(
            "INSERT INTO portal_documents "
            "(user_id, company, filename, original_name, mime_type, size_bytes, uploaded_by, job_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (uid, company, stored_name, secure_filename(f.filename), mime, size, uid, job_id),
        )
        saved += 1
    if saved:
        audit_log("job_doc_upload", target=f"job:{job_id}", metadata={"count": saved})
        flash(f"Uploaded {saved} file{'s' if saved != 1 else ''}.", "success")
    return redirect(dest)


def _job_doc_or_403(job_id, doc_id):
    """Fetch a job-scoped document, enforcing company + staffing access."""
    company = g.user["company"]
    role    = g.user["role"]
    uid     = int(g.user["sub"])
    _job_or_404(job_id, company)
    if role != "admin":
        from portal_jobs import is_staffed_on
        if role != "employee" or not is_staffed_on(job_id, uid):
            abort(403)
    doc = portal_db.query_one(
        "SELECT * FROM portal_documents WHERE id = %s AND company = %s AND job_id = %s",
        (doc_id, company, job_id),
    )
    if not doc:
        abort(404)
    return doc


@pages_bp.route("/portal/assignments/<int:job_id>/documents/<int:doc_id>/download")
@login_required
def download_job_document(job_id, doc_id):
    doc = _job_doc_or_403(job_id, doc_id)
    audit_log("job_doc_download", target=f"doc:{doc_id}",
              metadata={"job_id": job_id, "original_name": doc["original_name"]})
    return portal_storage.download(g.user["company"], doc["filename"], doc["original_name"])


@pages_bp.route("/portal/admin/assignments/<int:job_id>/documents/<int:doc_id>/delete", methods=["POST"])
@admin_required
def delete_job_document(job_id, doc_id):
    doc = _job_doc_or_403(job_id, doc_id)
    portal_storage.delete(g.user["company"], doc["filename"])
    portal_db.execute("DELETE FROM portal_documents WHERE id = %s", (doc_id,))
    audit_log("job_doc_delete", target=f"doc:{doc_id}",
              metadata={"job_id": job_id, "original_name": doc["original_name"]})
    flash("File deleted.", "success")
    return redirect(f"/portal/assignments/{job_id}")
