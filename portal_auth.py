import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
import jwt
from flask import (
    Blueprint, current_app, g, jsonify,
    make_response, redirect, render_template, request,
)

import portal_db

auth_bp = Blueprint("auth", __name__)


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(plaintext: str) -> str:
    return bcrypt.hashpw(plaintext.encode(), bcrypt.gensalt(rounds=12)).decode()


def check_password(plaintext: str, hashed: str) -> bool:
    return bcrypt.checkpw(plaintext.encode(), hashed.encode())


# ── JWT helpers ───────────────────────────────────────────────────────────────

def encode_jwt(payload: dict) -> str:
    data = dict(payload)
    data["exp"] = datetime.now(timezone.utc) + timedelta(hours=8)
    return jwt.encode(data, current_app.config["JWT_SECRET"], algorithm="HS256")


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, current_app.config["JWT_SECRET"], algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# ── Decorators ────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("portal_token")
        if not token:
            return redirect("/")
        payload = decode_jwt(token)
        if payload is None:
            resp = make_response(redirect("/"))
            resp.delete_cookie("portal_token")
            return resp
        g.user = payload
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


# ── Routes ────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["POST"])
def login():
    email    = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    company  = os.environ.get("COMPANY_ID", "dod")

    if not email or not password:
        return jsonify(ok=False, error="Email and password are required."), 400

    user = portal_db.query_one(
        "SELECT id, email, password_hash, role, company, full_name, active "
        "FROM portal_users WHERE email = %s AND company = %s",
        (email, company),
    )

    if not user or not user["active"] or not user["password_hash"]:
        return jsonify(ok=False, error="Invalid email or password."), 401

    if not check_password(password, user["password_hash"]):
        return jsonify(ok=False, error="Invalid email or password."), 401

    token = encode_jwt({
        "sub":     user["id"],
        "email":   user["email"],
        "role":    user["role"],
        "company": user["company"],
        "name":    user["full_name"] or user["email"],
    })

    resp = make_response(jsonify(ok=True, redirect="/portal"))
    resp.set_cookie(
        "portal_token", token,
        httponly=True,
        secure=not current_app.config.get("TESTING"),
        samesite="Lax",
        path="/",
        max_age=8 * 3600,
    )
    return resp


@auth_bp.route("/logout")
def logout():
    resp = make_response(redirect("/"))
    resp.delete_cookie("portal_token")
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

    # Always show success — prevents email enumeration
    return render_template("portal_forgot_password.html", sent=True)


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    company = os.environ.get("COMPANY_ID", "dod")
    user = portal_db.query_one(
        "SELECT id FROM portal_users "
        "WHERE reset_token = %s AND reset_expires > %s AND company = %s",
        (token, datetime.now(timezone.utc), company),
    )
    if not user:
        return render_template("portal_reset_password.html", invalid=True)

    if request.method == "GET":
        return render_template("portal_reset_password.html", token=token)

    password = request.form.get("password") or ""
    confirm  = request.form.get("confirm")  or ""

    if len(password) < 8:
        return render_template("portal_reset_password.html", token=token,
                               error="Password must be at least 8 characters.")
    if password != confirm:
        return render_template("portal_reset_password.html", token=token,
                               error="Passwords do not match.")

    portal_db.execute(
        "UPDATE portal_users SET password_hash = %s, reset_token = NULL, reset_expires = NULL "
        "WHERE id = %s",
        (hash_password(password), user["id"]),
    )
    return render_template("portal_reset_password.html", success=True)


@auth_bp.route("/setup-account/<token>", methods=["GET", "POST"])
def setup_account(token):
    company = os.environ.get("COMPANY_ID", "dod")
    user = portal_db.query_one(
        "SELECT id, full_name, email FROM portal_users "
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
    if len(password) < 8:
        return render_template("portal_setup_account.html", token=token, user=user,
                               error="Password must be at least 8 characters.")
    if password != confirm:
        return render_template("portal_setup_account.html", token=token, user=user,
                               error="Passwords do not match.")

    portal_db.execute(
        """UPDATE portal_users
           SET full_name = %s, phone = %s, company_name = %s, address = %s,
               password_hash = %s, active = TRUE,
               invite_token = NULL, invite_expires = NULL
           WHERE id = %s""",
        (full_name, phone, company_name, address, hash_password(password), user["id"]),
    )

    updated = portal_db.query_one(
        "SELECT id, email, role, company, full_name FROM portal_users WHERE id = %s",
        (user["id"],),
    )
    jwt_token = encode_jwt({
        "sub":     updated["id"],
        "email":   updated["email"],
        "role":    updated["role"],
        "company": updated["company"],
        "name":    updated["full_name"],
    })
    resp = make_response(redirect("/portal"))
    resp.set_cookie(
        "portal_token", jwt_token,
        httponly=True,
        secure=not current_app.config.get("TESTING"),
        samesite="Lax",
        path="/",
        max_age=8 * 3600,
    )
    return resp
