import os
import smtplib
import ssl
from email.message import EmailMessage


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


def _send_with_detail(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    """Same as _send but returns (ok, detail) so callers can surface the
    specific failure reason. detail is safe to display to admins."""
    cfg = _smtp_config()
    if cfg is None:
        return False, "SMTP is not configured. Set SMTP_HOST, SMTP_USER, and SMTP_PASS env vars."
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


def _send(to_email: str, subject: str, body: str) -> bool:
    ok, _detail = _send_with_detail(to_email, subject, body)
    return ok


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


def send_test_email(to_email: str, company_name: str) -> tuple[bool, str]:
    """Admin-triggered SMTP sanity check. Returns (ok, detail) for UI."""
    subject = f"{company_name} portal — SMTP test"
    body = (
        f"This is a test email from the {company_name} portal admin panel.\n\n"
        f"If you received it, your SMTP configuration is working — invite\n"
        f"and password-reset emails will deliver to client accounts.\n"
    )
    return _send_with_detail(to_email, subject, body)
