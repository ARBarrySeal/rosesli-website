"""Append-only audit log writer for security-relevant portal actions."""
from __future__ import annotations

import json
from typing import Any

from flask import g, has_request_context, request

import portal_db


def _client_ip() -> str | None:
    if not has_request_context():
        return None
    # Cloud Run / proxy chain — trust the leftmost forwarded-for value if set.
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.remote_addr


def _user_agent() -> str | None:
    if not has_request_context():
        return None
    ua = request.headers.get("User-Agent", "")
    return ua[:500] if ua else None


def log(
    action: str,
    *,
    target: str | None = None,
    user_id: int | str | None = None,
    email: str | None = None,
    company: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write one audit row. Never raises — failures are swallowed to avoid breaking
    the request that triggered the audit-worthy event.
    """
    try:
        if user_id is None and has_request_context() and hasattr(g, "user"):
            user_id = g.user.get("sub")
            email = email or g.user.get("email")
            company = company or g.user.get("company")
        try:
            user_id_int = int(user_id) if user_id is not None else None
        except (ValueError, TypeError):
            user_id_int = None

        portal_db.execute(
            "INSERT INTO portal_audit (user_id, email, company, action, target, ip, ua, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
            (
                user_id_int,
                email,
                company,
                action,
                target,
                _client_ip(),
                _user_agent(),
                json.dumps(metadata or {}),
            ),
        )
    except Exception:
        # Last-resort: never let audit failure break a real request.
        pass
