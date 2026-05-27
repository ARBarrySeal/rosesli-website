import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
import jwt
from flask import (
    Blueprint, current_app, g, jsonify,
    make_response, redirect, render_template, request, session,
)

import portal_db
import portal_mfa
from portal_audit import log as audit_log
from portal_limiter import limiter

auth_bp = Blueprint("auth", __name__)


# ── Lockout policy ────────────────────────────────────────────────────────────

LOCKOUT_THRESHOLD = 5            # failed attempts before lock
LOCKOUT_MINUTES   = 15           # how long the lock lasts
MFA_PENDING_MIN   = 5            # how long a /login → /mfa-challenge cookie lives
SESSION_HOURS     = 8            # full session lifetime


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=12)).decode()


def check_password(plaintext: str, hashed: str) -> bool:
    return bcrypt.checkpw(plaintext.encode(), hashed.encode())


import re

from portal_common_passwords import COMMON_PASSWORDS

# Strip trailing digits + common symbol-suffixes ("Password123!" → "password").
_PASSWORD_SUFFIX = re.compile(r"[\d!@#$%^&*_.?-]+$")


def _password_too_weak(pw: str) -> str | None:
    if len(pw) < 12:
        return "Password must be at least 12 characters."
    classes = sum([
        any(c.islower() for c in pw),
        any(c.isupper() for c in pw),
        any(c.isdigit() for c in pw),
        any(not c.isalnum() for c in pw),
    ])
    if classes < 3:
        return "Password must include 3 of: lowercase, uppercase, digit, symbol."
    lowered = pw.lower()
    base = _PASSWORD_SUFFIX.sub("", lowered)
    if lowered in COMMON_PASSWORDS or (base and base in COMMON_PASSWORDS):
        return "Password is too common — please choose something more unique."
    return None


# ── JWT helpers ───────────────────────────────────────────────────────────────

def encode_jwt(payload: dict, *, lifetime: timedelta | None = None) -> str:
    data = dict(payload)
    now = datetime.now(timezone.utc)
    data["iat"] = int(now.timestamp())
    data["exp"] = now + (lifetime or timedelta(hours=SESSION_HOURS))
    return jwt.encode(data, current_app.config["JWT_SECRET"], algorithm="HS256")


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, current_app.config["JWT_SECRET"], algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ── Decorators ────────────────────────────────────────────────────────────────

def _load_user_for_token(payload: dict) -> dict | None:
    """Fetch fresh user state and verify the JWT is still valid.

    Rejects tokens that were issued before the user's last password change, or
    that belong to a user who has been deactivated since the token was issued.
    """
    try:
        uid = int(payload.get("sub", ""))
    except (ValueError, TypeError):
        return None
    user = portal_db.query_one(
        "SELECT id, email, role, company, full_name, active, pw_changed_at, "
        "       locked_until, mfa_secret "
        "FROM portal_users WHERE id = %s",
        (uid,),
    )
    if not user or not user["active"]:
        return None
    iat = payload.get("iat")
    if iat is not None and user["pw_changed_at"]:
        if iat < int(user["pw_changed_at"].timestamp()):
            return None
    return user


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("portal_token")
        if not token:
            return redirect("/")
        payload = decode_jwt(token)
        # "pending" = mid-MFA-challenge; "enroll" = admin who hasn't finished
        # MFA setup. Neither grants access to protected routes — the
        # mfa_challenge and mfa_enroll routes use their own auth check.
        if payload is None or payload.get("mfa") in ("pending", "enroll"):
            resp = make_response(redirect("/"))
            resp.delete_cookie("portal_token")
            return resp
        user = _load_user_for_token(payload)
        if user is None:
            resp = make_response(redirect("/"))
            resp.delete_cookie("portal_token")
            return resp
        g.user = payload | {"db": user}
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if g.user.get("role") != "admin":
            return redirect("/portal")
        return f(*args, **kwargs)
    return decorated


# ── Token generation ──────────────────────────────────────────────────────────

def make_token() -> str:
    return secrets.token_urlsafe(32)


# ── Constants ─────────────────────────────────────────────────────────────────

COMPANY_NAMES = {
    "dod": "DoD Cyber Consulting",
    "rosesli": "Rose Sign Language Interpreting",
}


# ── Cookie helper ─────────────────────────────────────────────────────────────

def rotate_csrf_token() -> str:
    """Issue a new per-session CSRF token. Invalidates any token an attacker
    might have observed from the prior session state (pre-login,
    pre-MFA-completion, pre-password-change)."""
    session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


def _set_session_cookie(resp, token: str, *, max_age: int):
    # Every JWT issuance is a state transition (login, MFA, account setup);
    # rotate the CSRF token alongside so the old one stops validating.
    rotate_csrf_token()
    resp.set_cookie(
        "portal_token", token,
        httponly=True,
        secure=not current_app.config.get("TESTING"),
        samesite="Lax",
        path="/",
        max_age=max_age,
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["POST"])
@limiter.limit("10 per minute")
def login():
    email    = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    company  = os.environ.get("COMPANY_ID", "dod")

    if not email or not password:
        return jsonify(ok=False, error="Email and password are required."), 400

    user = portal_db.query_one(
        "SELECT id, email, password_hash, role, company, full_name, active, "
        "       failed_login_count, locked_until, mfa_secret "
        "FROM portal_users WHERE email = %s AND company = %s",
        (email, company),
    )

    now = datetime.now(timezone.utc)

    # Account locked? — refuse before even checking the password (no oracle).
    if user and user["locked_until"] and user["locked_until"] > now:
        audit_log("login_locked", email=email, company=company, user_id=user["id"])
        return jsonify(
            ok=False,
            error=f"Account temporarily locked. Try again after "
                  f"{user['locked_until'].astimezone(timezone.utc).strftime('%H:%M UTC')}.",
        ), 423

    bad_credentials = (
        not user
        or not user["active"]
        or not user["password_hash"]
        or not check_password(password, user["password_hash"])
    )

    if bad_credentials:
        if user:
            new_count = (user["failed_login_count"] or 0) + 1
            lock_until = (
                now + timedelta(minutes=LOCKOUT_MINUTES)
                if new_count >= LOCKOUT_THRESHOLD else None
            )
            portal_db.execute(
                "UPDATE portal_users SET failed_login_count = %s, locked_until = %s "
                "WHERE id = %s",
                (new_count, lock_until, user["id"]),
            )
            audit_log(
                "login_fail",
                email=email, company=company, user_id=user["id"],
                metadata={"attempts": new_count, "locked": bool(lock_until)},
            )
        else:
            audit_log("login_fail", email=email, company=company,
                      metadata={"reason": "unknown_email"})
        return jsonify(ok=False, error="Invalid email or password."), 401

    # Success: reset attempt counter and clear any prior lock.
    portal_db.execute(
        "UPDATE portal_users SET failed_login_count = 0, locked_until = NULL "
        "WHERE id = %s",
        (user["id"],),
    )

    # MFA disabled — every authenticated user gets a full session straight to the portal.
    token = encode_jwt({
        "sub":     str(user["id"]),
        "email":   user["email"],
        "role":    user["role"],
        "company": user["company"],
        "name":    user["full_name"] or user["email"],
        "mfa":     "n/a",
    })
    audit_log("login_ok", email=email, company=company, user_id=user["id"])
    resp = make_response(jsonify(ok=True, redirect="/portal"))
    _set_session_cookie(resp, token, max_age=SESSION_HOURS * 3600)
    return resp


@auth_bp.route("/mfa-challenge", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def mfa_challenge():
    token = request.cookies.get("portal_token")
    payload = decode_jwt(token) if token else None
    if not payload or payload.get("mfa") != "pending":
        return redirect("/")
    if request.method == "GET":
        return render_template("portal_mfa_challenge.html")

    code = (request.form.get("code") or "").strip()
    recovery = (request.form.get("recovery") or "").strip()
    try:
        uid = int(payload["sub"])
    except (ValueError, TypeError):
        return redirect("/")
    user = portal_db.query_one(
        "SELECT id, email, role, company, full_name, mfa_secret, mfa_recovery_hashes "
        "FROM portal_users WHERE id = %s AND active = TRUE",
        (uid,),
    )
    if not user:
        return redirect("/")

    accepted = False
    used_recovery = False

    if code and portal_mfa.verify_code(user["mfa_secret"] or "", code):
        accepted = True
    elif recovery:
        ok, remaining = portal_mfa.consume_recovery_code(
            json.dumps(user["mfa_recovery_hashes"]) if isinstance(user["mfa_recovery_hashes"], list)
            else (user["mfa_recovery_hashes"] or "[]"),
            recovery,
        )
        if ok:
            accepted = True
            used_recovery = True
            portal_db.execute(
                "UPDATE portal_users SET mfa_recovery_hashes = %s::jsonb WHERE id = %s",
                (json.dumps(remaining), user["id"]),
            )

    if not accepted:
        audit_log("mfa_fail", user_id=user["id"], email=user["email"], company=user["company"])
        return render_template("portal_mfa_challenge.html", error="Invalid code."), 401

    full_token = encode_jwt({
        "sub":     str(user["id"]),
        "email":   user["email"],
        "role":    user["role"],
        "company": user["company"],
        "name":    user["full_name"] or user["email"],
        "mfa":     "ok",
    })
    audit_log("mfa_ok", user_id=user["id"], email=user["email"], company=user["company"],
              metadata={"recovery": used_recovery})
    resp = make_response(redirect("/portal"))
    _set_session_cookie(resp, full_token, max_age=SESSION_HOURS * 3600)
    return resp


@auth_bp.route("/logout")
def logout():
    payload = None
    token = request.cookies.get("portal_token")
    if token:
        payload = decode_jwt(token)
    if payload and payload.get("sub"):
        audit_log("logout", user_id=payload.get("sub"), email=payload.get("email"),
                  company=payload.get("company"))
    resp = make_response(redirect("/"))
    resp.delete_cookie("portal_token", path="/")
    return resp


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("portal_forgot_password.html")

    email   = (request.form.get("email") or "").strip().lower()
    company = os.environ.get("COMPANY_ID", "dod")

    user = portal_db.query_one(
        "SELECT id, email, full_name FROM portal_users "
        "WHERE email = %s AND company = %s AND active = TRUE",
        (email, company),
    )
    if user:
        token   = make_token()
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        portal_db.execute(
            "UPDATE portal_users SET reset_token = %s, reset_expires = %s WHERE id = %s",
            (token, expires, user["id"]),
        )
        app_url   = os.environ.get("APP_URL", "http://localhost:8080")
        reset_url = f"{app_url}/reset-password/{token}"
        from portal_email import send_reset_email
        send_reset_email(user["email"], reset_url, COMPANY_NAMES.get(company, company))
        audit_log("pw_reset_request", user_id=user["id"], email=user["email"], company=company)

    return render_template("portal_forgot_password.html", sent=True)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    company = os.environ.get("COMPANY_ID", "dod")
    user = portal_db.query_one(
        "SELECT id, email FROM portal_users "
        "WHERE reset_token = %s AND reset_expires > %s AND company = %s",
        (token, datetime.now(timezone.utc), company),
    )
    if not user:
        return render_template("portal_reset_password.html", invalid=True)

    if request.method == "GET":
        return render_template("portal_reset_password.html", token=token)

    password = request.form.get("password") or ""
    confirm  = request.form.get("confirm")  or ""

    weak = _password_too_weak(password)
    if weak:
        return render_template("portal_reset_password.html", token=token, error=weak)
    if password != confirm:
        return render_template("portal_reset_password.html", token=token,
                               error="Passwords do not match.")

    portal_db.execute(
        "UPDATE portal_users "
        "SET password_hash = %s, reset_token = NULL, reset_expires = NULL, "
        "    pw_changed_at = NOW(), failed_login_count = 0, locked_until = NULL "
        "WHERE id = %s",
        (hash_password(password), user["id"]),
    )
    audit_log("pw_reset_complete", user_id=user["id"], email=user["email"], company=company)
    return render_template("portal_reset_password.html", success=True)


@auth_bp.route("/setup-account/<token>", methods=["GET", "POST"])
def setup_account(token):
    company = os.environ.get("COMPANY_ID", "dod")
    user = portal_db.query_one(
        "SELECT id, full_name, email, role FROM portal_users "
        "WHERE invite_token = %s AND invite_expires > %s AND company = %s AND active = FALSE",
        (token, datetime.now(timezone.utc), company),
    )
    if not user:
        return render_template("portal_setup_account.html", invalid=True)

    if request.method == "GET":
        return render_template("portal_setup_account.html", token=token, user=user)

    full_name    = (request.form.get("full_name")    or "").strip()
    phone        = (request.form.get("phone")        or "").strip()
    company_name = (request.form.get("company_name") or "").strip()
    address      = (request.form.get("address")      or "").strip()
    password     = request.form.get("password") or ""
    confirm      = request.form.get("confirm")  or ""

    if not full_name:
        return render_template("portal_setup_account.html", token=token, user=user,
                               error="Full name is required.")
    weak = _password_too_weak(password)
    if weak:
        return render_template("portal_setup_account.html", token=token, user=user, error=weak)
    if password != confirm:
        return render_template("portal_setup_account.html", token=token, user=user,
                               error="Passwords do not match.")

    portal_db.execute(
        """UPDATE portal_users
           SET full_name = %s, phone = %s, company_name = %s, address = %s,
               password_hash = %s, active = TRUE, pw_changed_at = NOW(),
               invite_token = NULL, invite_expires = NULL
           WHERE id = %s""",
        (full_name, phone, company_name, address, hash_password(password), user["id"]),
    )
    audit_log("account_setup", user_id=user["id"], email=user["email"], company=company)

    updated = portal_db.query_one(
        "SELECT id, email, role, company, full_name FROM portal_users WHERE id = %s",
        (user["id"],),
    )

    jwt_token = encode_jwt({
        "sub":     str(updated["id"]),
        "email":   updated["email"],
        "role":    updated["role"],
        "company": updated["company"],
        "name":    updated["full_name"],
        "mfa":     "n/a",
    })
    resp = make_response(redirect("/portal"))
    _set_session_cookie(resp, jwt_token, max_age=SESSION_HOURS * 3600)
    return resp
