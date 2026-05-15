import json
import logging
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

from flask import Flask, jsonify, redirect, request, send_from_directory

app = Flask(__name__, static_folder=".", static_url_path="/static")
logging.basicConfig(level=logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/about.html")
@app.route("/about")
def about():
    return send_from_directory(BASE_DIR, "about.html")


@app.route("/testimonials.html")
@app.route("/testimonials")
def testimonials():
    return send_from_directory(BASE_DIR, "testimonials.html")


@app.route("/specialties.html")
@app.route("/specialties")
def specialties_redirect():
    return redirect("/testimonials", code=301)


@app.route("/vri.html")
@app.route("/vri")
def vri():
    return send_from_directory(BASE_DIR, "vri.html")


@app.route("/request.html")
@app.route("/request")
def request_page():
    return send_from_directory(BASE_DIR, "request.html")


@app.route("/accessibility-statement.html")
@app.route("/accessibility-statement")
def accessibility():
    return send_from_directory(BASE_DIR, "accessibility-statement.html")


@app.route("/robots.txt")
def robots():
    return send_from_directory(BASE_DIR, "robots.txt", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    return send_from_directory(BASE_DIR, "sitemap.xml", mimetype="application/xml")


@app.route("/favicon.svg")
def favicon():
    return send_from_directory(BASE_DIR, "favicon.svg", mimetype="image/svg+xml")


@app.route("/site.css")
def css():
    return send_from_directory(BASE_DIR, "site.css", mimetype="text/css")


_ALLOWED_IMAGES = frozenset({
    "hand-shaka.png",
    "hand-iloveyou.png",
    "nad-logo.png",
    "nic-logo.png",
    "rid-logo.png",
})


@app.route("/<path:filename>.png")
def image(filename):
    name = f"{filename}.png"
    if name not in _ALLOWED_IMAGES:
        return ("Not found", 404)
    return send_from_directory(BASE_DIR, name, mimetype="image/png")


FIELD_LABELS = [
    ("name", "Name"),
    ("org", "Organization"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("setting", "Setting"),
    ("client_count", "Deaf clients"),
    ("format", "Format"),
    ("date", "Date"),
    ("start_time", "Start"),
    ("end_time", "End"),
    ("details", "Details"),
]


def _build_message(form):
    lines = []
    for key, label in FIELD_LABELS:
        val = (form.get(key) or "").strip()
        if val:
            lines.append(f"{label}: {val}")
    extra_dates = [v for k, v in form.items() if k.startswith("date_") and v]
    if extra_dates:
        lines.append("Additional dates: " + ", ".join(extra_dates))
    return "\n".join(lines)


def _structured_log(severity, **fields):
    print(json.dumps({"severity": severity, **fields}), flush=True)


def _log_submission(form, delivered, method, error=None):
    submission = {key: form.get(key, "").strip() for key, _ in FIELD_LABELS if form.get(key)}
    extra_dates = [v for k, v in form.items() if k.startswith("date_") and v]
    if extra_dates:
        submission["additional_dates"] = extra_dates
    _structured_log(
        "ERROR" if error else "INFO",
        message="interpreter_request_submission",
        delivered=delivered,
        delivery_method=method,
        submission=submission,
        error=str(error) if error else None,
    )


def _send_via_resend(body, reply_to):
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return None
    payload = {
        "from": os.environ.get("RESEND_FROM", "Rose's Li <onboarding@resend.dev>"),
        "to": [os.environ.get("RESEND_TO", "info@rosesli.com")],
        "subject": "New interpreter request — rosesli.com",
        "text": body,
    }
    if reply_to:
        payload["reply_to"] = [reply_to]
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return 200 <= resp.status < 300


def _send_via_smtp(body, reply_to):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    if not (host and user and password):
        return None
    port = int(os.environ.get("SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["Subject"] = "New interpreter request — rosesli.com"
    msg["From"] = user
    msg["To"] = os.environ.get("SMTP_TO", "info@rosesli.com")
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(user, password)
        smtp.send_message(msg)
    return True


@app.route("/api/request", methods=["POST"])
def api_request():
    form = request.form
    if not form.get("name") or not form.get("email"):
        return jsonify(ok=False, error="missing_required"), 400
    body = _build_message(form)
    reply_to = form.get("email")
    method = "none"
    delivered = False
    try:
        result = _send_via_resend(body, reply_to)
        if result is not None:
            method, delivered = "resend", result
        else:
            result = _send_via_smtp(body, reply_to)
            if result is not None:
                method, delivered = "smtp", result
    except Exception as exc:
        _log_submission(form, False, method or "error", error=exc)
        return jsonify(ok=False, error="send_failed"), 502
    _log_submission(form, delivered, method)
    return jsonify(ok=True, delivered=delivered)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
