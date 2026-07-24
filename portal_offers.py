"""Scheduler — job offers (Usked-style coordinator-offers matching).

Flow: coordinator (admin) offers a job to one or more available interpreters →
interpreters accept/decline in their portal → coordinator confirms one accepted
interpreter, which snapshots them onto the job and withdraws sibling offers.

Double-booking guard: an interpreter already confirmed on another job with the
same event_date cannot be offered/accepted/confirmed again for that date.

Company-scoped throughout (g.user["company"]); DoD and Rose SLI never mix.
"""
from datetime import datetime

from flask import (
    Blueprint, abort, flash, g, redirect, render_template, request, url_for,
)

import portal_db
import portal_email
from portal_audit import log as audit_log
from portal_auth import admin_required, login_required
from portal_availability import available_interpreters, is_unavailable
from portal_jobs import _name_for, add_job_interpreter

offers_bp = Blueprint("offers", __name__)

_COMPANY_NAMES = {"rosesli": "Rose Sign Language Interpreting", "dod": "DOD Cyber Consulting"}

# Admin Job Offers page groups every assignment by its status. Ordered + labelled
# here so the page reads coordinator-first (needs staffing → booked → done).
_OFFER_CATEGORIES = (
    ("pending",   "Needs staffing"),
    ("confirmed", "Confirmed"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _company_name(company: str) -> str:
    return _COMPANY_NAMES.get(company, company)


def _abs_url(path: str) -> str:
    """Absolute URL for email deep links, derived from the live request host."""
    return request.url_root.rstrip("/") + path


def _job(job_id: int, company: str) -> dict | None:
    return portal_db.query_one(
        "SELECT * FROM jobs WHERE id = %s AND company = %s", (job_id, company)
    )


def _confirmed_elsewhere(interpreter_id: int, company: str, event_date, exclude_job_id=None) -> bool:
    """True if the interpreter is already confirmed on a different job that day."""
    if event_date is None:
        return False
    row = portal_db.query_one(
        "SELECT 1 FROM jobs "
        "WHERE company = %s AND status = 'confirmed' AND event_date = %s "
        "AND id <> %s "
        "AND (interpreter_1_id = %s OR interpreter_2_id = %s "
        "     OR EXISTS (SELECT 1 FROM job_interpreters ji "
        "                WHERE ji.job_id = jobs.id AND ji.interpreter_id = %s)) "
        "LIMIT 1",
        (company, event_date, exclude_job_id or -1, interpreter_id, interpreter_id, interpreter_id),
    )
    return row is not None


def _offers_for_job(job_id: int, company: str) -> list:
    """Per-interpreter offer rows for a job, newest activity first."""
    return portal_db.query_all(
        "SELECT o.*, u.full_name AS interpreter_name, u.email AS interpreter_email "
        "FROM job_offers o JOIN portal_users u ON u.id = o.interpreter_id "
        "WHERE o.job_id = %s AND o.company = %s "
        "ORDER BY o.offered_at DESC",
        (job_id, company),
    )


def offers_for_job(job_id: int, company: str) -> list:
    """Public alias used by the assignment-detail template."""
    return _offers_for_job(job_id, company)


def pending_offer_count(interpreter_id: int, company: str) -> int:
    row = portal_db.query_one(
        "SELECT COUNT(*) AS n FROM job_offers "
        "WHERE interpreter_id = %s AND company = %s AND status = 'offered'",
        (interpreter_id, company),
    )
    return int(row["n"]) if row else 0


def active_interpreter_emails(company: str) -> list:
    """Every active, non-archived interpreter's id/email/name for this company —
    the "Interpreter email list" the Phase 6 broadcast-offer blast sends to."""
    return portal_db.query_all(
        "SELECT id, email, full_name FROM portal_users "
        "WHERE company = %s AND role = 'employee' AND active = TRUE "
        "AND (archived IS NULL OR archived = FALSE) "
        "ORDER BY full_name NULLS LAST",
        (company,),
    )


# ── Coordinator side (admin) ──────────────────────────────────────────────────

@offers_bp.route("/portal/admin/assignments/<int:job_id>/offer", methods=["POST"])
@admin_required
def offer_job(job_id):
    company = g.user["company"]
    job = _job(job_id, company)
    if not job:
        abort(404)

    ids = request.form.getlist("interpreter_id", type=int)
    if not ids:
        flash("Select at least one interpreter to offer.", "error")
        return redirect(f"/portal/admin/assignments/{job_id}")

    note = (request.form.get("note") or "").strip() or None
    offered, skipped = [], []
    for iid in ids:
        # Don't offer to someone already booked or blocked off that day.
        if _confirmed_elsewhere(iid, company, job.get("event_date"), exclude_job_id=job_id):
            skipped.append((iid, "already booked"))
            continue
        if is_unavailable(iid, company, job.get("event_date")):
            skipped.append((iid, "unavailable"))
            continue

        # Upsert: re-offering a previously declined/withdrawn interpreter resets them.
        row = portal_db.execute(
            "INSERT INTO job_offers (company, job_id, interpreter_id, status, note) "
            "VALUES (%s, %s, %s, 'offered', %s) "
            "ON CONFLICT (job_id, interpreter_id) DO UPDATE SET "
            "status = 'offered', note = EXCLUDED.note, "
            "offered_at = NOW(), responded_at = NULL "
            "RETURNING id",
            (company, job_id, iid, note),
        )
        offered.append(iid)

        # Notify: email + in-portal (the offer row itself is the in-portal notice).
        person = portal_db.query_one(
            "SELECT full_name, email FROM portal_users WHERE id = %s AND company = %s",
            (iid, company),
        )
        if person and person.get("email"):
            portal_email.send_offer_email(
                person["email"], person["full_name"] or "there", job,
                _abs_url("/portal/offers"), _company_name(company),
            )

    audit_log("job_offer", target=f"job:{job_id}",
              metadata={"offered": offered, "skipped": skipped})

    if offered:
        flash(f"Offered to {len(offered)} interpreter(s).", "success")
    if skipped:
        names = ", ".join(f"{_name_for(i, company) or f'#{i}'} ({why})" for i, why in skipped)
        flash(f"Skipped {len(skipped)}: {names}.", "error")
    return redirect(f"/portal/admin/assignments/{job_id}")


@offers_bp.route("/portal/admin/offers/<int:offer_id>/withdraw", methods=["POST"])
@admin_required
def withdraw_offer(offer_id):
    company = g.user["company"]
    offer = portal_db.query_one(
        "SELECT id, job_id, interpreter_id FROM job_offers WHERE id = %s AND company = %s",
        (offer_id, company),
    )
    if not offer:
        abort(404)
    portal_db.execute(
        "UPDATE job_offers SET status = 'withdrawn', responded_at = NOW() "
        "WHERE id = %s AND company = %s",
        (offer_id, company),
    )
    audit_log("job_offer_withdraw", target=f"offer:{offer_id}")

    # Notify the interpreter they've been unassigned.
    job = _job(offer["job_id"], company)
    person = portal_db.query_one(
        "SELECT full_name, email FROM portal_users WHERE id = %s AND company = %s",
        (offer["interpreter_id"], company),
    )
    if job and person and person.get("email"):
        portal_email.send_withdraw_email(
            person["email"], person["full_name"] or "there", job,
            _abs_url("/portal/offers"), _company_name(company),
        )

    flash("Offer withdrawn.", "success")
    return redirect(f"/portal/admin/assignments/{offer['job_id']}")


@offers_bp.route("/portal/admin/assignments/<int:job_id>/confirm", methods=["POST"])
@admin_required
def confirm_interpreter(job_id):
    company = g.user["company"]
    job = _job(job_id, company)
    if not job:
        abort(404)

    iid = request.form.get("interpreter_id", type=int)
    if not iid:
        flash("Pick an interpreter to confirm.", "error")
        return redirect(f"/portal/admin/assignments/{job_id}")

    # Must have accepted (idempotent: confirming an already-confirmed person is fine).
    offer = portal_db.query_one(
        "SELECT id, status FROM job_offers "
        "WHERE job_id = %s AND interpreter_id = %s AND company = %s",
        (job_id, iid, company),
    )
    if not offer or offer["status"] not in ("accepted", "offered"):
        flash("That interpreter has no accepted offer to confirm.", "error")
        return redirect(f"/portal/admin/assignments/{job_id}")

    if _confirmed_elsewhere(iid, company, job.get("event_date"), exclude_job_id=job_id):
        flash("That interpreter is already confirmed on another job that day.", "error")
        return redirect(f"/portal/admin/assignments/{job_id}")

    name = _name_for(iid, company)
    # Staff at the next open slot (never overwrites anyone already confirmed),
    # then mark the chosen offer accepted.
    filled = add_job_interpreter(company, job_id, iid)
    portal_db.execute(
        "UPDATE job_offers SET status = 'accepted', responded_at = NOW() "
        "WHERE id = %s AND company = %s",
        (offer["id"], company),
    )

    # The job flips to confirmed — and sibling offers get withdrawn — only once
    # every needed slot is filled (num_required, falling back to the requested
    # count, then 1). Until then remaining offers stay open.
    required = job.get("num_required") or job.get("num_interpreters") or 1
    fully_staffed = filled >= required
    if fully_staffed:
        portal_db.execute(
            "UPDATE jobs SET status = 'confirmed' WHERE id = %s AND company = %s",
            (job_id, company),
        )
        portal_db.execute(
            "UPDATE job_offers SET status = 'withdrawn', responded_at = NOW() "
            "WHERE job_id = %s AND company = %s AND id <> %s "
            "AND status IN ('offered', 'accepted') AND interpreter_id NOT IN "
            "(SELECT interpreter_id FROM job_interpreters WHERE job_id = %s)",
            (job_id, company, offer["id"], job_id),
        )
        # Idempotent (mig 012 unique index on job_id): re-confirming can never
        # bill the same assignment twice.
        from portal_client_invoices import ensure_invoice_for_job
        ensure_invoice_for_job(company, job_id, int(g.user["sub"]))
    audit_log("job_confirm", target=f"job:{job_id}",
              metadata={"interpreter_id": iid, "filled": filled, "required": required})

    person = portal_db.query_one(
        "SELECT full_name, email FROM portal_users WHERE id = %s AND company = %s",
        (iid, company),
    )
    if person and person.get("email"):
        portal_email.send_confirm_email(
            person["email"], person["full_name"] or "there", job,
            _abs_url("/portal/offers"), _company_name(company),
        )
    if fully_staffed:
        flash(f"Confirmed {name}. Job fully staffed ({filled} of {required}).", "success")
    else:
        flash(f"Confirmed {name} ({filled} of {required} slots filled).", "success")
    return redirect(f"/portal/admin/assignments/{job_id}")


# ── Interpreter side (employee) ───────────────────────────────────────────────

def _admin_offers_view(company: str):
    """Coordinator Job Offers page: every assignment, grouped by status, with the
    count of still-open offers per job so the office sees what needs action."""
    rows = portal_db.query_all(
        "SELECT j.id, j.job_number, j.event_date, j.start_time, j.end_time, "
        "       j.setting, j.event_address, j.event_zip, j.client_name, "
        "       j.requester_name, j.interpreter_1_name, j.interpreter_2_name, j.status, "
        "       (SELECT COUNT(*) FROM job_offers o "
        "          WHERE o.job_id = j.id AND o.company = j.company "
        "            AND o.status = 'offered') AS open_offers "
        "FROM jobs j WHERE j.company = %s "
        "ORDER BY j.event_date NULLS LAST, j.start_time",
        (company,),
    )
    by_status: dict[str, list] = {}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)

    groups = [
        {"status": s, "label": label, "rows": by_status.pop(s, [])}
        for s, label in _OFFER_CATEGORIES
    ]
    # Surface any status not in our known list rather than silently dropping it.
    for s, leftover in by_status.items():
        groups.append({"status": s, "label": (s or "Other").capitalize(), "rows": leftover})

    return render_template("portal_offers_admin.html", groups=groups, total=len(rows))


@offers_bp.route("/portal/offers")
@login_required
def my_offers():
    company = g.user["company"]
    role    = g.user["role"]
    if role not in ("admin", "employee"):
        abort(403)
    if role == "admin":
        return _admin_offers_view(company)
    uid = int(g.user["sub"])

    pending = portal_db.query_all(
        "SELECT o.*, j.event_date, j.start_time, j.end_time, j.setting, "
        "       j.event_address, j.event_zip, j.job_number "
        "FROM job_offers o JOIN jobs j ON j.id = o.job_id "
        "WHERE o.interpreter_id = %s AND o.company = %s AND o.status = 'offered' "
        "ORDER BY j.event_date NULLS LAST, o.offered_at",
        (uid, company),
    )
    history = portal_db.query_all(
        "SELECT o.*, j.event_date, j.start_time, j.end_time, j.setting, "
        "       j.event_address, j.event_zip, j.job_number "
        "FROM job_offers o JOIN jobs j ON j.id = o.job_id "
        "WHERE o.interpreter_id = %s AND o.company = %s AND o.status <> 'offered' "
        "ORDER BY o.responded_at DESC NULLS LAST LIMIT 25",
        (uid, company),
    )
    return render_template("portal_offers.html", pending=pending, history=history)


def _respond(offer_id: int, decision: str):
    """Shared accept/decline handler. decision ∈ {'accepted','declined'}."""
    company = g.user["company"]
    uid     = int(g.user["sub"])

    offer = portal_db.query_one(
        "SELECT o.*, j.event_date FROM job_offers o JOIN jobs j ON j.id = o.job_id "
        "WHERE o.id = %s AND o.company = %s",
        (offer_id, company),
    )
    if not offer:
        abort(404)
    # Interpreters may only act on their own pending offers.
    if offer["interpreter_id"] != uid:
        abort(403)
    if offer["status"] != "offered":
        flash("That offer is no longer open.", "error")
        return redirect("/portal/offers")

    if decision == "accepted" and _confirmed_elsewhere(uid, company, offer["event_date"]):
        flash("You're already confirmed on another job that day.", "error")
        return redirect("/portal/offers")

    portal_db.execute(
        "UPDATE job_offers SET status = %s, responded_at = NOW() "
        "WHERE id = %s AND company = %s",
        (decision, offer_id, company),
    )
    audit_log(f"offer_{decision}", target=f"offer:{offer_id}", metadata={"job_id": offer["job_id"]})

    # Notify the coordinator(s) so they can confirm.
    job = _job(offer["job_id"], company)
    verb = "accepted" if decision == "accepted" else "declined"
    me = _name_for(uid, company) or "An interpreter"
    link = _abs_url(f"/portal/admin/assignments/{offer['job_id']}")
    for email in portal_email.coordinator_recipients(company):
        portal_email.send_offer_response_email(
            email, me, verb, job or {}, link, _company_name(company)
        )

    flash(f"Offer {verb}.", "success")
    return redirect("/portal/offers")


@offers_bp.route("/portal/offers/<int:offer_id>/accept", methods=["POST"])
@login_required
def accept_offer(offer_id):
    return _respond(offer_id, "accepted")


@offers_bp.route("/portal/offers/<int:offer_id>/decline", methods=["POST"])
@login_required
def decline_offer(offer_id):
    return _respond(offer_id, "declined")
