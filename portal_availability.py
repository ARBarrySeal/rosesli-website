"""Scheduler — interpreter availability (block-off-unavailable model).

Interpreters mark the dates they CANNOT work; the absence of a block means
available. The coordinator's offer UI reuses `available_interpreters()` to filter
the picker down to people who are free on the job's date. Company-scoped like the
rest of the portal so DoD never sees Rose SLI availability.
"""
from flask import (
    Blueprint, abort, flash, g, redirect, render_template, request,
)

import portal_db
from portal_audit import log as audit_log
from portal_auth import login_required
from portal_jobs import _date_or_none, _interpreters

availability_bp = Blueprint("availability", __name__)


# ── Helpers reused by the offer flow ──────────────────────────────────────────

def _iso(event_date) -> str | None:
    """Normalize a date (date object or ISO string) to 'YYYY-MM-DD', or None."""
    if event_date is None:
        return None
    if hasattr(event_date, "isoformat"):
        return event_date.isoformat()
    return _date_or_none(str(event_date))


def is_unavailable(user_id: int, company: str, event_date) -> bool:
    """True if the interpreter has a block covering event_date."""
    iso = _iso(event_date)
    if iso is None:
        return False
    row = portal_db.query_one(
        "SELECT 1 FROM interpreter_unavailability "
        "WHERE user_id = %s AND company = %s AND start_date <= %s AND end_date >= %s "
        "LIMIT 1",
        (user_id, company, iso, iso),
    )
    return row is not None


def available_interpreters(company: str, event_date) -> list:
    """`_interpreters(company)` minus anyone blocked off on event_date.

    With no date we can't filter, so every active interpreter is returned.
    """
    interps = _interpreters(company)
    iso = _iso(event_date)
    if iso is None:
        return interps
    blocked = {
        r["user_id"]
        for r in portal_db.query_all(
            "SELECT DISTINCT user_id FROM interpreter_unavailability "
            "WHERE company = %s AND start_date <= %s AND end_date >= %s",
            (company, iso, iso),
        )
    }
    return [i for i in interps if i["id"] not in blocked]


def _blocks_for(user_id: int, company: str) -> list:
    from datetime import date
    return portal_db.query_all(
        "SELECT * FROM interpreter_unavailability "
        "WHERE user_id = %s AND company = %s AND end_date >= %s "
        "ORDER BY start_date",
        (user_id, company, date.today().isoformat()),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@availability_bp.route("/portal/availability")
@login_required
def availability():
    company = g.user["company"]
    role    = g.user["role"]
    if role not in ("admin", "employee"):
        abort(403)

    # Admins may inspect a specific interpreter's calendar via ?user_id=.
    target_id = int(g.user["sub"])
    target_name = g.user.get("name")
    if role == "admin":
        qs_uid = request.args.get("user_id", type=int)
        if qs_uid:
            target_id = qs_uid
            row = portal_db.query_one(
                "SELECT full_name FROM portal_users WHERE id = %s AND company = %s",
                (qs_uid, company),
            )
            target_name = row["full_name"] if row else f"#{qs_uid}"

    return render_template(
        "portal_availability.html",
        blocks=_blocks_for(target_id, company),
        target_id=target_id,
        target_name=target_name,
        is_admin=(role == "admin"),
        interpreters=_interpreters(company) if role == "admin" else [],
    )


@availability_bp.route("/portal/availability/add", methods=["POST"])
@login_required
def add_block():
    company = g.user["company"]
    role    = g.user["role"]
    if role not in ("admin", "employee"):
        abort(403)

    # Whose calendar: self, unless an admin explicitly targets an interpreter.
    user_id = int(g.user["sub"])
    if role == "admin":
        posted = request.form.get("user_id", type=int)
        if posted:
            user_id = posted

    start = _date_or_none(request.form.get("start_date"))
    end   = _date_or_none(request.form.get("end_date")) or start
    if not start:
        flash("A start date is required.", "error")
        return redirect("/portal/availability")
    if end < start:
        end = start

    all_day = request.form.get("all_day") != "0"
    start_time = None if all_day else (request.form.get("start_time") or "").strip() or None
    end_time   = None if all_day else (request.form.get("end_time") or "").strip() or None
    note = (request.form.get("note") or "").strip() or None

    row = portal_db.execute(
        "INSERT INTO interpreter_unavailability "
        "(company, user_id, start_date, end_date, all_day, start_time, end_time, note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (company, user_id, start, end, all_day, start_time, end_time, note),
    )
    audit_log("availability_add", target=f"unavail:{row['id'] if row else '?'}",
              metadata={"user_id": user_id, "start": start, "end": end})
    flash("Unavailability saved.", "success")
    dest = "/portal/availability"
    if role == "admin" and user_id != int(g.user["sub"]):
        dest += f"?user_id={user_id}"
    return redirect(dest)


@availability_bp.route("/portal/availability/<int:block_id>/delete", methods=["POST"])
@login_required
def delete_block(block_id):
    company = g.user["company"]
    role    = g.user["role"]
    uid     = int(g.user["sub"])

    block = portal_db.query_one(
        "SELECT id, user_id FROM interpreter_unavailability WHERE id = %s AND company = %s",
        (block_id, company),
    )
    if not block:
        abort(404)
    # Interpreters can only delete their own blocks; admins, anyone in-company.
    if role != "admin" and block["user_id"] != uid:
        abort(403)

    portal_db.execute(
        "DELETE FROM interpreter_unavailability WHERE id = %s AND company = %s",
        (block_id, company),
    )
    audit_log("availability_delete", target=f"unavail:{block_id}")
    flash("Unavailability removed.", "success")
    dest = "/portal/availability"
    if role == "admin" and block["user_id"] != uid:
        dest += f"?user_id={block['user_id']}"
    return redirect(dest)
