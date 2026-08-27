import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

import portal_db


def _smtp_config() -> dict | None:
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not (host and user and password):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user,
        "password": password,
        "from": os.environ.get("SMTP_FROM", user),
    }


def _send_via_resend(to_email: str, subject: str, body: str) -> tuple[bool, str] | None:
    """Send through Resend's HTTP API.

    Returns None when Resend isn't configured so the caller falls through to
    SMTP. Mirrors the provider call already used by the public request form."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return None
    payload = {
        "from": os.environ.get("RESEND_FROM", "Rose's Li <onboarding@resend.dev>"),
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend's anti-abuse appears to flag urllib's default UA from Cloud Run.
            "User-Agent": "rosesli-website/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                return True, f"Sent via Resend as {payload['from']}."
            return False, f"Resend returned HTTP {resp.status}."
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return False, f"Resend error (HTTP {exc.code}): {detail}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"Resend connection error: {type(exc).__name__}: {exc}"


def _send_via_smtp(to_email: str, subject: str, body: str) -> tuple[bool, str] | None:
    """Send through SMTP. Returns None when SMTP isn't configured."""
    cfg = _smtp_config()
    if cfg is None:
        return None
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = to_email
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
        return True, f"Sent via {cfg['host']}:{cfg['port']} as {cfg['from']}."
    except smtplib.SMTPAuthenticationError as e:
        err = e.smtp_error.decode(errors="replace") if e.smtp_error else str(e)
        return False, f"SMTP auth failed ({e.smtp_code}): {err}"
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {type(e).__name__}: {e}"
    except (OSError, ssl.SSLError) as e:
        return False, f"Connection error: {type(e).__name__}: {e}"


def _log(severity: str, **fields) -> None:
    """Cloud Run reads single-line JSON on stdout as a structured log entry."""
    print(json.dumps({"severity": severity, **fields}), flush=True)


def _send_with_detail(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Deliver a portal email, preferring Resend and falling back to SMTP.

    Returns (ok, detail); detail is safe to display to admins. Every failure is
    logged: a silently swallowed SMTP auth error previously made undelivered
    credential mail indistinguishable from a successful send."""
    attempts: list[str] = []
    for provider, sender in (("resend", _send_via_resend), ("smtp", _send_via_smtp)):
        result = sender(to_email, subject, body)
        if result is None:
            continue
        ok, detail = result
        if ok:
            return True, detail
        attempts.append(f"{provider}: {detail}")
        _log("ERROR", message=f"{provider}_send_failed", to=to_email, detail=detail)
    if not attempts:
        return False, ("Email is not configured. Set RESEND_API_KEY, or "
                       "SMTP_HOST, SMTP_USER and SMTP_PASS.")
    summary = " | ".join(attempts)
    _log("ERROR", message="email_send_failed", to=to_email, detail=summary)
    return False, summary


def _send(to_email: str, subject: str, body: str) -> bool:
    ok, _detail = _send_with_detail(to_email, subject, body)
    return ok


def _route_recipient(to_email: str, body: str) -> tuple[str, str]:
    """While testing, PORTAL_TEST_EMAIL diverts a message away from the real
    recipient so live interpreters/clients aren't emailed. Returns the address
    to actually send to plus a body that notes the original recipient."""
    test_to = os.environ.get("PORTAL_TEST_EMAIL")
    if test_to and test_to != to_email:
        body = f"[TEST MODE] Original recipient: {to_email}\n\n{body}"
        return test_to, body
    return to_email, body


def coordinator_recipients(company: str) -> list[str]:
    """Who receives coordinator/admin notifications for a company.

    When PORTAL_ADMIN_NOTIFY_EMAIL is set, every admin notification goes to that
    single inbox — this is how Rose SLI routes all coordinator alerts to Amanda
    without editing individual user records. Otherwise it falls back to the
    company's active admin users."""
    override = os.environ.get("PORTAL_ADMIN_NOTIFY_EMAIL", "").strip()
    if override:
        return [override]
    rows = portal_db.query_all(
        "SELECT email FROM portal_users "
        "WHERE company = %s AND role = 'admin' AND active = TRUE AND email IS NOT NULL",
        (company,),
    )
    return [r["email"] for r in rows]


def send_invite_email(to_email: str, to_name: str, setup_url: str, company_name: str) -> bool:
    subject = f"You're invited to the {company_name} client portal"
    body = (
        f"Hi {to_name},\n\n"
        f"You've been invited to the {company_name} portal.\n\n"
        f"Click the link below to set up your account. This link expires in 48 hours.\n\n"
        f"{setup_url}\n\n"
        f"If you didn't expect this email, please ignore it.\n\n"
        f"— {company_name}\n"
    )
    return _send(to_email, subject, body)


def send_reset_email(to_email: str, reset_url: str, company_name: str) -> bool:
    subject = f"{company_name} — Password Reset"
    body = (
        f"Hi,\n\n"
        f"We received a request to reset your password for the {company_name} portal.\n\n"
        f"Click the link below to set a new password. This link expires in 1 hour.\n\n"
        f"{reset_url}\n\n"
        f"If you didn't request this, you can safely ignore this email.\n\n"
        f"— {company_name}\n"
    )
    return _send(to_email, subject, body)


def send_default_password_email(to_email: str, to_name: str, login_url: str,
                                company_name: str) -> bool:
    subject = f"Your {company_name} portal account is ready"
    body = (
        f"Hi {to_name},\n\n"
        f"Your {company_name} portal account has been created.\n\n"
        f"Sign in with your email address and the temporary password below —\n"
        f"you'll be asked to create your own password right away.\n\n"
        f"  Temporary password: password\n\n"
        f"{login_url}\n\n"
        f"If you didn't expect this email, please ignore it.\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


def send_temp_password_email(to_email: str, temp_password: str, login_url: str,
                             company_name: str) -> bool:
    subject = f"{company_name} — Your temporary password"
    body = (
        f"Hi,\n\n"
        f"A temporary password was issued for your {company_name} portal account.\n\n"
        f"  Temporary password: {temp_password}\n\n"
        f"Sign in here and you'll be asked to create a new password right away:\n\n"
        f"{login_url}\n\n"
        f"If you didn't request this, contact us before signing in.\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


def send_test_email(to_email: str, company_name: str) -> tuple[bool, str]:
    """Admin-triggered SMTP sanity check. Returns (ok, detail) for UI."""
    subject = f"{company_name} portal — SMTP test"
    body = (
        f"This is a test email from the {company_name} portal admin panel.\n\n"
        f"If you received it, your SMTP configuration is working — invite\n"
        f"and password-reset emails will deliver to client accounts.\n"
    )
    return _send_with_detail(to_email, subject, body)


# ── Scheduler: job offers ─────────────────────────────────────────────────────

def _job_number_bit(job: dict) -> str | None:
    """Job # (2026-07-22 batch, Phase 9) — the padded job_number, not the
    raw internal row id, matching the label used everywhere else in the
    portal (assignments list, offers, dashboards, invoices, calendar)."""
    n = job.get("job_number")
    return f"Job #{n:03d}" if n else None


def _job_when(job: dict) -> str:
    """One-line Job #/date/time/location summary for offer/confirm emails."""
    bits = []
    jn = _job_number_bit(job)
    if jn:
        bits.append(jn)
    d = job.get("event_date")
    if d:
        bits.append(d.strftime("%A, %B %d, %Y") if hasattr(d, "strftime") else str(d))
    times = " – ".join(t for t in (job.get("start_time"), job.get("end_time")) if t)
    if times:
        bits.append(times)
    where = job.get("event_address") or job.get("event_zip")
    if where:
        bits.append(where)
    return " · ".join(bits) if bits else "details in the portal"


def send_offer_email(to_email: str, to_name: str, job: dict, offers_url: str,
                     company_name: str) -> bool:
    subject = f"New assignment offer — {_job_when(job)}"
    body = (
        f"Hi {to_name},\n\n"
        f"You've been offered an assignment:\n\n"
        f"  {_job_when(job)}\n"
        f"  Setting: {job.get('setting') or '—'}\n\n"
        f"Please accept or decline in the portal:\n\n"
        f"{offers_url}\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


def _job_when_zip_only(job: dict) -> str:
    """Schedule summary for the broadcast blast (Phase 6, 2026-07-22 batch) —
    zip code only, NEVER the full street address (6.2), since a broadcast
    fans out to every active interpreter, not just people the coordinator
    specifically vetted for this job."""
    bits = []
    jn = _job_number_bit(job)
    if jn:
        bits.append(jn)
    d = job.get("event_date")
    if d:
        bits.append(d.strftime("%A, %B %d, %Y") if hasattr(d, "strftime") else str(d))
    times = " – ".join(t for t in (job.get("start_time"), job.get("end_time")) if t)
    if times:
        bits.append(times)
    zip_code = job.get("event_zip")
    if zip_code:
        bits.append(f"ZIP {zip_code}")
    return " · ".join(bits) if bits else "details in the portal"


def send_broadcast_offer_email(to_email: str, to_name: str, job: dict, offers_url: str,
                               company_name: str) -> bool:
    """"Broadcast to all interpreters" (Phase 6, 2026-07-22 batch) — same
    offer mechanics as send_offer_email, but the schedule line is zip-only
    (see _job_when_zip_only) since it goes out to everyone at once."""
    subject = f"New assignment available — {_job_when_zip_only(job)}"
    body = (
        f"Hi {to_name},\n\n"
        f"A new assignment is open and available on a first-come basis:\n\n"
        f"  {_job_when_zip_only(job)}\n"
        f"  Setting: {job.get('setting') or '—'}\n\n"
        f"Claim it in the portal:\n\n"
        f"{offers_url}\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


def send_offer_accepted_confirmation_email(to_email: str, to_name: str, job: dict,
                                           staffed_names: list, doc_names: list,
                                           portal_url: str, company_name: str) -> bool:
    """Phase 8 (2026-07-22 batch, 8.6) — sent to the interpreter themselves the
    moment they accept an offer (before the office's separate confirm step).
    Full location detail is fine here (unlike the broadcast blast) since
    they've already committed to this specific job. Documents are linked,
    not attached — consistent with every other email in this system."""
    subject = f"You accepted — {_job_when(job)}"
    interp_line = ", ".join(staffed_names) if staffed_names else "Unassigned"
    body = (
        f"Hi {to_name},\n\n"
        f"You accepted this assignment. The office will confirm it shortly:\n\n"
        f"  {_job_when(job)}\n"
        f"  Client: {job.get('client_name') or '—'}\n"
        f"  Interpreters assigned: {interp_line}\n"
    )
    if job.get("interpreter_notes"):
        body += f"  Notes: {job['interpreter_notes']}\n"
    body += "\nDocuments:\n"
    body += "".join(f"  - {d}\n" for d in doc_names) if doc_names else "  (none)\n"
    body += (
        f"\nView full details in the portal:\n\n{portal_url}\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


def send_confirm_email(to_email: str, to_name: str, job: dict, offers_url: str,
                       company_name: str) -> bool:
    subject = f"You're confirmed — {_job_when(job)}"
    body = (
        f"Hi {to_name},\n\n"
        f"You're confirmed for this assignment:\n\n"
        f"  {_job_when(job)}\n"
        f"  Setting: {job.get('setting') or '—'}\n\n"
        f"Details are in your portal:\n\n"
        f"{offers_url}\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


def send_withdraw_email(to_email: str, to_name: str, job: dict, offers_url: str,
                        company_name: str) -> bool:
    """Notify an interpreter that they've been unassigned/withdrawn from a job."""
    subject = f"Assignment update — {_job_when(job)}"
    body = (
        f"Hi {to_name},\n\n"
        f"You have been unassigned from this job.  Thank you.\n\n"
        f"  {_job_when(job)}\n"
        f"  Setting: {job.get('setting') or '—'}\n\n"
        f"You can view your current offers here:\n\n"
        f"{offers_url}\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


def send_offer_response_email(to_email: str, interpreter_name: str, decision: str,
                              job: dict, link_url: str, company_name: str) -> bool:
    """Notify the coordinator that an interpreter accepted/declined an offer."""
    subject = f"{interpreter_name} {decision} — {_job_when(job)}"
    body = (
        f"{interpreter_name} {decision} the assignment offer:\n\n"
        f"  {_job_when(job)}\n\n"
        f"Review and confirm in the portal:\n\n"
        f"{link_url}\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


# ── Interpreter master invoice ────────────────────────────────────────────────

def send_master_invoice_email(to_email: str, interpreter_name: str, invoice_id: int,
                              total: float, lines: list[str], link_url: str,
                              company_name: str) -> bool:
    """Notify the coordinator that an interpreter bundled assignments into a
    master invoice and submitted it for payment."""
    subject = f"Invoice submitted for payment — {interpreter_name} (${total:.2f})"
    line_text = "\n".join(f"  {ln}" for ln in lines) if lines else "  (no line detail)"
    body = (
        f"{interpreter_name} submitted a master invoice for payment:\n\n"
        f"  Invoice #{invoice_id}\n"
        f"{line_text}\n\n"
        f"  Total: ${total:.2f}\n\n"
        f"Review it in the portal:\n\n"
        f"{link_url}\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


def send_invoice_submitted_email(to_email: str, interpreter_name: str, invoice_id: int,
                                 amount: float, link_url: str, company_name: str) -> bool:
    """Notify the coordinator that an interpreter locked and submitted an
    individual invoice for review (Interpreter Invoices — Submit for Review)."""
    subject = f"Invoice submitted for review — {interpreter_name} (${amount:.2f})"
    body = (
        f"{interpreter_name} submitted invoice #{invoice_id} for review "
        f"(${amount:.2f}). It is now locked from further edits.\n\n"
        f"Review it in the portal:\n\n"
        f"{link_url}\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)


def send_invoice_paid_email(to_email: str, interpreter_name: str, invoice_id: int,
                            amount: float, link_url: str, company_name: str) -> bool:
    """Close the loop: tell the interpreter their submitted invoice was paid."""
    subject = f"Your invoice was paid — ${amount:.2f}"
    body = (
        f"Hi {interpreter_name},\n\n"
        f"Good news — your invoice #{invoice_id} for ${amount:.2f} has been "
        f"marked paid.\n\n"
        f"View it in the portal:\n\n"
        f"{link_url}\n\n"
        f"— {company_name}\n"
    )
    to_email, body = _route_recipient(to_email, body)
    return _send(to_email, subject, body)
