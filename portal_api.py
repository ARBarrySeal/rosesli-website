import secrets
from flask import Blueprint, g, jsonify, session
from portal_auth import login_required

api_bp = Blueprint("api", __name__)


@api_bp.route("/api/csrf-token")
def csrf_token_endpoint():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return jsonify(csrf_token=session["csrf_token"])


@api_bp.route("/api/me")
@login_required
def me():
    return jsonify(
        ok=True,
        user={
            "id":    g.user["sub"],
            "email": g.user["email"],
            "name":  g.user.get("name", g.user["email"]),
            "role":  g.user["role"],
        },
    )
