"""Client invoice CRUD — Rose SLI portal (admin only create/edit)."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request

import portal_db
from portal_audit import log as audit_log
from portal_auth import admin_required, login_required

client_inv_bp = Blueprint("client_invoices", __name__)

DIFFERENTIALS = {
    "day":                ("Daytime / Weekdays 7a–5p",   0),
    "weekend_day":        ("Weekend Day (Sat/Sun) 7a–5p",  5),
    "weekday_evening":    ("Weekday Evening 5p–10p",        5),
    "weekend_evening":    ("Weekend Evening 5p–10p",       10),
    "overnight":          ("Overnight 10p–7a",             10),
    "weekend_overnight":  ("Weekend Overnight 10p–7a",     15),
    "conference":         ("Conference",                   15),
    "holiday":            ("Holiday",                      12),
    "lmr":                ("LMR (<24 hr notice)",          10),
}

HOLIDAYS = (
    "New Year's Day", "MLK Day", "Presidents' Day", "Cesar Chavez Day",
    "Memorial Day", "Juneteenth", "Independence Day", "Labor Day",
    "Veterans Day", "Thanksgiving Day", "Christmas Day",
)


def _clients(company):
    return portal_db.query_all(
        "SELECT id, full_name, email FROM portal_users "
        "WHERE company = %s AND role = 'client' AND active = TRUE "
        "ORDER BY full_name",
        (company,),
    )


def _float_or_none(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _date_or_none(raw):
    from datetime import date
    raw = (raw or "").strip()
    try:
        date.fromisoformat(raw)
        return raw
    except ValueError:
        return None


@client_inv_bp.route("/portal/client-invoices")
@login_required
def client_invoices_list():
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        rows = portal_db.query_all(
            "SELECT ci.*, u.full_name AS user_full_name "
            "FROM client_invoices ci "
            "LEFT JOIN portal_users u ON u.id = ci.client_id "
            "WHERE ci.company = %s ORDER BY ci.created_at DESC",
            (company,),
        )
    elif role == "client":
        rows = portal_db.query_all(
            "SELECT * FROM client_invoices "
            "WHERE company = %s AND client_id = %s ORDER BY created_at DESC",
            (company, uid),
        )
    else:
        rows = []

    return render_template("portal_client_invoices.html", invoices=rows)


@client_inv_bp.route("/portal/client-invoices/<int:inv_id>")
@login_required
def client_invoice_detail(inv_id):
    company = g.user["company"]
    role    = g.user["role"]
    uid     = g.user["sub"]

    if role == "admin":
        inv = portal_db.query_one(
            "SELECT ci.*, u.full_name AS user_full_name "
            "FROM client_invoices ci "
            "LEFT JOIN portal_users u ON u.id = ci.client_id "
            "WHERE ci.id = %s AND ci.company = %s",
            (inv_id, company),
        )
    elif role == "client":
        inv = portal_db.query_one(
            "SELECT * FROM client_invoices "
            "WHERE id = %s AND company = %s AND client_id = %s",
            (inv_id, company, uid),
        )
    else:
        abort(403)

    if not inv:
        abort(404)
    return render_template("portal_client_invoice.html", inv=inv)


@client_inv_bp.route("/portal/admin/client-invoices/create", methods=["GET", "POST"])
@admin_required
def create_client_invoice():
    company = g.user["company"]
    clients = _clients(company)

    if request.method == "GET":
        return render_template("portal_client_invoice_create.html",
                               clients=clients)

    client_id = request.form.get("client_id") or None
    if client_id:
        target = portal_db.query_one(
            "SELECT id, full_name FROM portal_users WHERE id = %s AND company = %s",
            (int(client_id), company),
        )
        if not target:
            return render_template("portal_client_invoice_create.html",
                                   clients=clients, error="Invalid client.")
        client_name = target["full_name"]
        client_id   = target["id"]
    else:
        client_name = (request.form.get("client_name") or "").strip()
        client_id   = None

    poc_email       = (request.form.get("poc_email") or "").strip() or None
    poc_phone       = (request.form.get("poc_phone") or "").strip() or None
    date_of_service = _date_or_none(request.form.get("date_of_service"))
    start_time      = (request.form.get("start_time") or "").strip() or None
    end_time        = (request.form.get("end_time") or "").strip() or None
    duration_hours  = _float_or_none(request.form.get("duration_hours"))
    rate_per_hour   = _float_or_none(request.form.get("rate_per_hour"))
    incidentals     = _float_or_none(request.form.get("incidentals")) or 0.0
    notes           = (request.form.get("notes") or "").strip() or None

    if not rate_per_hour:
        return render_template("portal_client_invoice_create.html",
                               clients=clients, error="Rate per hour is required.")

    total = None
    if duration_hours and rate_per_hour:
        total = round(duration_hours * rate_per_hour + incidentals, 2)

    portal_db.execute(
        "INSERT INTO client_invoices "
        "(company, client_id, client_name, poc_email, poc_phone, date_of_service, "
        "start_time, end_time, duration_hours, rate_per_hour, incidentals, total, "
        "notes, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (company, client_id, client_name, poc_email, poc_phone, date_of_service,
         start_time, end_time, duration_hours, rate_per_hour, incidentals, total,
         notes, int(g.user["sub"])),
    )
    audit_log("client_invoice_create",
              metadata={"client_name": client_name, "total": total})
    flash("Client invoice created.", "success")
    return redirect("/portal/client-invoices")


@client_inv_bp.route("/portal/admin/client-invoices/<int:inv_id>/mark-paid",
                     methods=["POST"])
@admin_required
def mark_client_invoice_paid(inv_id):
    company = g.user["company"]
    inv = portal_db.query_one(
        "SELECT id FROM client_invoices WHERE id = %s AND company = %s",
        (inv_id, company),
    )
    if not inv:
        abort(404)
    paid_date = request.form.get("paid_date") or None
    portal_db.execute(
        "UPDATE client_invoices SET status='paid', paid_date=%s WHERE id=%s",
        (paid_date, inv_id),
    )
    audit_log("client_invoice_paid", target=f"client_invoice:{inv_id}",
              metadata={"paid_date": paid_date})
    flash("Invoice marked as paid.", "success")
    return redirect(f"/portal/client-invoices/{inv_id}")


@client_inv_bp.route("/portal/admin/client-invoices/<int:inv_id>/mark-unpaid",
                     methods=["POST"])
@admin_required
def mark_client_invoice_unpaid(inv_id):
    company = g.user["company"]
    inv = portal_db.query_one(
        "SELECT id FROM client_invoices WHERE id = %s AND company = %s",
        (inv_id, company),
    )
    if not inv:
        abort(404)
    portal_db.execute(
        "UPDATE client_invoices SET status='unpaid', paid_date=NULL WHERE id=%s",
        (inv_id,),
    )
    audit_log("client_invoice_unpaid", target=f"client_invoice:{inv_id}")
    flash("Invoice marked as unpaid.", "success")
    return redirect(f"/portal/client-invoices/{inv_id}")
