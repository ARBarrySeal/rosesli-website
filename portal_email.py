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


def _send(to_email: str, subject: str, body: str) -> bool:
    cfg = _smtp_config()
    if cfg is None:
        return False
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
        return True
    except Exception:
        return False


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
