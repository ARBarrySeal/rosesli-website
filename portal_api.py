from flask import Blueprint, g, jsonify
from portal_auth import login_required

api_bp = Blueprint("api", __name__)


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
