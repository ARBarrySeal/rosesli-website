import hmac
import json
import logging
import os
import secrets
import smtplib
import ssl
import urllib.error
import urllib.request
from datetime import timedelta
from email.message import EmailMessage

from flask import Flask, g, jsonify, redirect, request, send_from_directory, session

_testing = os.environ.get("TESTING") == "1"

_jwt_secret = os.environ.get("JWT_SECRET")
if not _jwt_secret:
    if _testing:
        _jwt_secret = "test-jwt-secret"
    else:
        raise RuntimeError("JWT_SECRET environment variable must be set")

_flask_secret = os.environ.get("FLASK_SECRET_KEY")
if not _flask_secret:
    if _testing:
        _flask_secret = "test-flask-secret"
    else:
        raise RuntimeError("FLASK_SECRET_KEY environment variable must be set")

app = Flask(__name__, static_folder=".", static_url_path="/static")
app.config["JWT_SECRET"]                 = _jwt_secret
app.secret_key                           = _flask_secret
app.config["TESTING"]                    = _testing
app.config["SESSION_COOKIE_HTTPONLY"]    = True
app.config["SESSION_COOKIE_SECURE"]      = not _testing
app.config["SESSION_COOKIE_SAMESITE"]    = "Lax"
app.config["SESSION_COOKIE_PATH"]        = "/"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)
logging.basicConfig(level=logging.INFO)

from portal_limiter import limiter
if _testing:
    app.config["RATELIMIT_ENABLED"] = False
limiter.init_app(app)

import portal_db
from portal_auth import auth_bp
from portal_api import api_bp
from portal_admin import admin_bp
from portal_pages import pages_bp
from portal_jobs import jobs_bp
from portal_client_invoices import client_inv_bp
from portal_availability import availability_bp
from portal_offers import offers_bp
from portal_interpreter_invoices import interp_inv_bp
app.register_blueprint(auth_bp)
app.register_blueprint(api_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(pages_bp)
app.register_blueprint(jobs_bp)
app.register_blueprint(client_inv_bp)
app.register_blueprint(availability_bp)
app.register_blueprint(offers_bp)
app.register_blueprint(interp_inv_bp)

# ── CSRF ──────────────────────────────────────────────────────────────────────

def _csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]

app.jinja_env.globals["csrf_token"] = _csrf_token


# ── CSP nonce ─────────────────────────────────────────────────────────────────
# Per-request nonce for inline <script>/<style> blocks. Eliminates the
# 'unsafe-inline' source for script-src in the portal CSP — any injected
# <script> without our nonce will be blocked by the browser.

def _csp_nonce():
    if not hasattr(g, "csp_nonce"):
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce

app.jinja_env.globals["csp_nonce"] = _csp_nonce


@app.context_processor
def _inject_company():
    """Tenant id for standalone (pre-auth) templates that can't read g.user."""
    return {"company_id": os.environ.get("COMPANY_ID", "dod")}


from portal_pages import CERT_OPTIONS, SPECIALTY_OPTIONS, US_STATES  # noqa: E402
app.jinja_env.globals["us_states"] = US_STATES
app.jinja_env.globals["cert_options"] = CERT_OPTIONS
app.jinja_env.globals["specialty_options"] = SPECIALTY_OPTIONS


def _list_name(u):
    """'Last, First' for lists when the split columns exist (rosesli saves and
    backfill populate them); falls back to full_name so dod rows are unchanged."""
    if u.get("last_name") and u.get("first_name"):
        return f"{u['last_name']}, {u['first_name']}"
    return u.get("full_name") or "—"

app.jinja_env.globals["list_name"] = _list_name


@app.template_filter("hours_minutes")
def _hours_minutes(value):
    """Decimal-hours ('2.5') → '2 hr 30 min'. Whole hours drop the minutes
    ('2' → '2 hr'), sub-hour values drop the hours ('0.75' → '45 min'), and
    unparseable values pass through unchanged so legacy free-text durations
    ('2 hours') still display."""
    if value is None or str(value).strip() == "":
        return value
    try:
        minutes = round(float(value) * 60)
    except (TypeError, ValueError):
        return value
    hr, mn = divmod(minutes, 60)
    if hr and mn:
        return f"{hr} hr {mn} min"
    if hr:
        return f"{hr} hr"
    return f"{mn} min"


@app.context_processor
def _inject_offer_badge():
    """Nav badge counts: pending offers (interpreters/admins), plus the
    unactioned public-request inbox (admins only)."""
    user = getattr(g, "user", None)
    if not user or user.get("role") not in ("admin", "employee"):
        return {}
    counts = {}
    try:
        from portal_offers import pending_offer_count
        counts["pending_offers"] = pending_offer_count(int(user["sub"]), user["company"])
    except Exception:
        counts["pending_offers"] = 0
    if user.get("role") == "admin":
        try:
            from portal_jobs import pending_request_count
            counts["pending_requests"] = pending_request_count(user["company"])
        except Exception:
            counts["pending_requests"] = 0
    return counts


_CSRF_EXEMPT = {"/api/request"}

@app.before_request
def csrf_protect():
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return
    if request.path in _CSRF_EXEMPT:
        return
    token    = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    expected = session.get("csrf_token", "")
    if not token or not expected or not hmac.compare_digest(token, expected):
        from flask import abort
        abort(403)

# ── Security headers ──────────────────────────────────────────────────────────

_PORTAL_PATH_PREFIXES = (
    "/portal",
    "/login",
    "/logout",
    "/mfa-challenge",
    "/forgot-password",
    "/reset-password",
    "/setup-account",
)


def _is_portal_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in _PORTAL_PATH_PREFIXES)


@app.after_request
def security_headers(response):
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]     = "camera=(), microphone=(), geolocation=()"

    # Two CSPs: strict (nonce-only script-src) for the portal where every
    # inline <script> is under our control and carries the nonce; permissive
    # (unsafe-inline) for the marketing pages which include hand-written
    # inline scripts that cannot be noncified (static HTML, no Jinja).
    if _is_portal_path(request.path):
        nonce = _csp_nonce()
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://assets.calendly.com; "
            "style-src 'self' 'unsafe-inline' https://assets.calendly.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "frame-src https://calendly.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://calendly.com;"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://assets.calendly.com "
            "https://www.googletagmanager.com https://www.google-analytics.com; "
            "style-src 'self' 'unsafe-inline' https://assets.calendly.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "frame-src https://calendly.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://calendly.com https://www.google-analytics.com "
            "https://*.google-analytics.com https://*.analytics.google.com;"
        )
    return response

# ── Canonical redirect ────────────────────────────────────────────────────────
# Page canonicals and the sitemap declare the non-www apex (rosesli.com) as the
# canonical host. 301 any www traffic to it so ranking signal consolidates on
# one hostname instead of splitting across www / non-www.

@app.before_request
def redirect_to_canonical():
    host = request.host.split(":")[0]
    if host == "www.rosesli.com":
        path = request.full_path.rstrip("?")
        return redirect(f"https://rosesli.com{path}", code=301)


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


@app.route("/medical-interpreting.html")
@app.route("/medical-interpreting")
def medical_interpreting():
    return send_from_directory(BASE_DIR, "medical-interpreting.html")


@app.route("/legal-interpreting.html")
@app.route("/legal-interpreting")
def legal_interpreting():
    return send_from_directory(BASE_DIR, "legal-interpreting.html")


@app.route("/educational-interpreting.html")
@app.route("/educational-interpreting")
def educational_interpreting():
    return send_from_directory(BASE_DIR, "educational-interpreting.html")


@app.route("/request.html")
@app.route("/request")
def request_page():
    return send_from_directory(BASE_DIR, "request.html")


@app.route("/accessibility-statement.html")
@app.route("/accessibility-statement")
def accessibility():
    return send_from_directory(BASE_DIR, "accessibility-statement.html")


@app.route("/blog.html")
@app.route("/blog")
def blog():
    return send_from_directory(BASE_DIR, "blog.html")


@app.route("/blog/<slug>")
def blog_article(slug):
    # Serve an individual Journal article from blog/<slug>.html. Sanitize to a
    # bare slug (alnum + hyphen) so it can't traverse out of blog/, and fall
    # back to the Journal hub for unknown slugs.
    safe = "".join(c for c in slug.lower() if c.isalnum() or c == "-")
    fname = f"{safe}.html"
    blog_dir = os.path.join(BASE_DIR, "blog")
    if safe and os.path.isfile(os.path.join(blog_dir, fname)):
        return send_from_directory(blog_dir, fname)
    return redirect("/blog", code=301)


@app.route("/robots.txt")
def robots():
    return send_from_directory(BASE_DIR, "robots.txt", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    return send_from_directory(BASE_DIR, "sitemap.xml", mimetype="application/xml")


# Static, rarely-changed assets (logos, CSS, photos) get a day of browser
# caching -- previously served with no caching directive at all (Flask's
# send_file defaults to Cache-Control: no-cache when max_age isn't passed),
# so every repeat visit re-fetched them from scratch. HTML pages stay
# uncached since those are edited regularly and should always be fresh.
_STATIC_ASSET_MAX_AGE = 86400


@app.route("/favicon.svg")
def favicon():
    return send_from_directory(BASE_DIR, "favicon.svg", mimetype="image/svg+xml",
                               max_age=_STATIC_ASSET_MAX_AGE)


@app.route("/favicon.ico")
def favicon_ico():
    return send_from_directory(BASE_DIR, "favicon.svg", mimetype="image/svg+xml",
                               max_age=_STATIC_ASSET_MAX_AGE)


@app.route("/logo.svg")
def bimi_logo():
    return send_from_directory(BASE_DIR, "logo.svg", mimetype="image/svg+xml",
                               max_age=_STATIC_ASSET_MAX_AGE)


@app.route("/site.css")
def css():
    return send_from_directory(BASE_DIR, "site.css", mimetype="text/css",
                               max_age=_STATIC_ASSET_MAX_AGE)


_ALLOWED_IMAGES = frozenset({
    "hand-shaka.png",
    "hand-shaka.webp",
    "hand-shaka.jpg",
    "hand-iloveyou.png",
    "nad-logo.png",
    "nic-logo.png",
    "rid-logo.png",
    "og-image.png",
    "amanda-rose.jpg",
})


@app.route("/<path:filename>.png")
def image(filename):
    name = f"{filename}.png"
    if name not in _ALLOWED_IMAGES:
        return ("Not found", 404)
    return send_from_directory(BASE_DIR, name, mimetype="image/png",
                               max_age=_STATIC_ASSET_MAX_AGE)


@app.route("/<path:filename>.webp")
def image_webp(filename):
    name = f"{filename}.webp"
    if name not in _ALLOWED_IMAGES:
        return ("Not found", 404)
    return send_from_directory(BASE_DIR, name, mimetype="image/webp",
                               max_age=_STATIC_ASSET_MAX_AGE)


@app.route("/<path:filename>.jpg")
def image_jpg(filename):
    name = f"{filename}.jpg"
    if name not in _ALLOWED_IMAGES:
        return ("Not found", 404)
    return send_from_directory(BASE_DIR, name, mimetype="image/jpeg",
                               max_age=_STATIC_ASSET_MAX_AGE)


FIELD_LABELS = [
    ("name", "Requester name"),
    ("org", "Organization"),
    ("email", "Requester email"),
    ("phone", "Requester phone"),
    ("setting", "Setting"),
    ("client_count", "Deaf clients"),
    ("format", "Format"),
    ("dress_code", "Dress code"),
    ("street", "Event street"),
    ("city", "Event city"),
    ("state", "Event state"),
    ("zip", "Event ZIP"),
    ("date", "Date"),
    ("start_time", "Start"),
    ("end_time", "End"),
    ("poc_name", "On-site POC"),
    ("poc_phone", "POC phone"),
    ("poc_email", "POC email"),
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
        "to": [os.environ.get("RESEND_TO", "amandarose@rosesli.com")],
        "subject": "New interpreter request — rosesli.com",
        "text": body,
    }
    if reply_to:
        payload["reply_to"] = [reply_to]
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
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        # Capture Resend's error body so structured logs explain *why* it failed.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"resend_http_{exc.code}: {detail}") from exc


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
    msg["To"] = os.environ.get("SMTP_TO", "amandarose@rosesli.com")
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(user, password)
        smtp.send_message(msg)
    return True


def _valid_date(raw):
    """Return raw if it's an ISO date (YYYY-MM-DD) the jobs.event_date column accepts, else None."""
    raw = (raw or "").strip()
    try:
        from datetime import date
        date.fromisoformat(raw)
        return raw
    except ValueError:
        return None


# Shared with request.html's dropdown — keep the two lists in sync.
DRESS_CODES = ["Casual", "Business Casual", "Business/Office", "Semi-formal", "Formal"]


def _create_job_from_request(form):
    """Auto-create a pending job from a public interpreter request.

    Rose SLI only. Best-effort: a DB failure here must never break the public
    request (email is the critical path), so all errors are swallowed + logged.
    """
    company = os.environ.get("COMPANY_ID", "dod")
    if company != "rosesli":
        return
    extra_dates = [v for k, v in form.items() if k.startswith("date_") and v]
    notes = (form.get("details") or "").strip()
    if extra_dates:
        notes = (notes + "\n" if notes else "") + "Additional dates: " + ", ".join(extra_dates)
    # Public endpoint: only allowlisted dress codes / state codes reach the DB.
    dress = (form.get("dress_code") or "").strip()
    if dress not in DRESS_CODES:
        dress = ""
    state = (form.get("state") or "").strip().upper()
    if state not in US_STATES:
        state = ""
    try:
        portal_db.execute(
            "INSERT INTO jobs (company, status, source, requester_name, requester_email, "
            "requester_phone, organization, setting, service_format, event_zip, deaf_clients, "
            "event_date, start_time, end_time, notes, "
            "dress_code, event_street, event_city, event_state, "
            "poc_name, poc_email, poc_phone) "
            "VALUES (%s, 'pending', 'public_request', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s)",
            (
                company,
                (form.get("name") or "").strip() or None,
                (form.get("email") or "").strip() or None,
                (form.get("phone") or "").strip() or None,
                (form.get("org") or "").strip() or None,
                (form.get("setting") or "").strip() or None,
                (form.get("format") or "").strip() or None,
                (form.get("zip") or "").strip() or None,
                (form.get("client_count") or "").strip() or None,
                _valid_date(form.get("date")),
                (form.get("start_time") or "").strip() or None,
                (form.get("end_time") or "").strip() or None,
                notes or None,
                dress or None,
                (form.get("street") or "").strip() or None,
                (form.get("city") or "").strip() or None,
                state or None,
                (form.get("poc_name") or "").strip() or None,
                (form.get("poc_email") or "").strip() or None,
                (form.get("poc_phone") or "").strip() or None,
            ),
        )
    except Exception as exc:
        _structured_log("ERROR", message="job_autocreate_failed", error=str(exc))


@app.route("/api/request", methods=["POST"])
@limiter.limit("12 per hour")
def api_request():
    form = request.form
    # Honeypot: the "website" field is hidden from real visitors via CSS, so any
    # value means a bot filled it. Accept-and-drop (return success, send nothing)
    # so the bot believes it succeeded and doesn't retry or escalate.
    if (form.get("website") or "").strip():
        _structured_log("INFO", message="interpreter_request_honeypot_drop")
        return jsonify(ok=True, delivered=False)
    if not form.get("name") or not form.get("email"):
        return jsonify(ok=False, error="missing_required"), 400
    body = _build_message(form)
    reply_to = form.get("email")
    method = "none"
    delivered = False
    last_error = None
    _diag_resend_error = None
    _diag_smtp_error = None
    # Email is best-effort. Each provider gets its own try so a Resend failure
    # still allows the SMTP fallback to run, and a total email failure does not
    # block persisting the lead to the DB or showing the user success.
    try:
        result = _send_via_resend(body, reply_to)
        if result is not None:
            method, delivered = "resend", result
    except Exception as exc:
        last_error = exc
        _diag_resend_error = str(exc)
        _structured_log("ERROR", message="resend_send_failed", error=str(exc))
    if not delivered:
        try:
            result = _send_via_smtp(body, reply_to)
            if result is not None:
                method, delivered = "smtp", result
        except Exception as exc:
            last_error = exc
            _diag_smtp_error = str(exc)
            _structured_log("ERROR", message="smtp_send_failed", error=str(exc))
    _create_job_from_request(form)
    _log_submission(form, delivered, method, error=last_error)
    resp = {"ok": True, "delivered": delivered}
    # TEMP diagnostic (2026-08-11): surface provider errors when a matching debug
    # header is sent, to unblock investigating why delivery is failing without
    # log/console access. Remove this block once the root cause is fixed.
    if request.headers.get("X-Diag-Token") == "rosesli-tmp-debug-20260811":
        resp["diag"] = {
            "resend_configured": bool(os.environ.get("RESEND_API_KEY")),
            "smtp_configured": bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS")),
            "resend_error": _diag_resend_error,
            "smtp_error": _diag_smtp_error,
            "method": method,
        }
    return jsonify(**resp)


@app.route("/portal.css")
def portal_css():
    return send_from_directory(BASE_DIR, "portal.css", mimetype="text/css")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
