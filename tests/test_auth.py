import portal_db


def test_db_query_one_returns_none_for_missing():
    result = portal_db.query_one(
        "SELECT id FROM portal_users WHERE email = %s",
        ("no-such-user@example.com",),
    )
    assert result is None


def test_db_query_all_returns_empty_list():
    result = portal_db.query_all(
        "SELECT id FROM portal_users WHERE email = %s",
        ("no-such-user@example.com",),
    )
    assert isinstance(result, list)
    assert len(result) == 0


def test_hash_password_produces_different_hashes():
    from portal_auth import hash_password
    h1 = hash_password("secret")
    h2 = hash_password("secret")
    assert h1 != h2


def test_check_password_correct():
    from portal_auth import hash_password, check_password
    h = hash_password("mypassword")
    assert check_password("mypassword", h) is True


def test_check_password_wrong():
    from portal_auth import hash_password, check_password
    h = hash_password("mypassword")
    assert check_password("wrongpassword", h) is False


def test_encode_decode_jwt(app):
    from portal_auth import encode_jwt, decode_jwt
    with app.app_context():
        token = encode_jwt({"sub": 42, "role": "client", "company": "dod", "email": "a@b.com"})
        payload = decode_jwt(token)
    assert payload["sub"] == 42
    assert payload["role"] == "client"


def test_decode_invalid_jwt(app):
    from portal_auth import decode_jwt
    with app.app_context():
        result = decode_jwt("not.a.real.token")
    assert result is None


def test_send_invite_email_returns_false_when_smtp_not_configured(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    from portal_email import send_invite_email
    result = send_invite_email(
        to_email="test@example.com",
        to_name="Test User",
        setup_url="http://localhost/setup-account/abc123",
        company_name="DoD Cyber Consulting",
    )
    assert result is False


def test_send_reset_email_returns_false_when_smtp_not_configured(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    from portal_email import send_reset_email
    result = send_reset_email(
        to_email="test@example.com",
        reset_url="http://localhost/reset-password/abc123",
        company_name="DoD Cyber Consulting",
    )
    assert result is False


def test_login_returns_json_error_for_unknown_email(client):
    resp = client.post("/login", data={"email": "nobody@example.com", "password": "bad"})
    assert resp.content_type == "application/json"
    data = resp.get_json()
    assert data["ok"] is False


def test_logout_redirects_and_clears_cookie(client):
    resp = client.get("/logout")
    assert resp.status_code == 302
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert "portal_token" in set_cookie


def test_api_me_returns_401_when_not_logged_in(client):
    resp = client.get("/api/me")
    assert resp.status_code == 401


def test_forgot_password_page_loads(client):
    resp = client.get("/forgot-password")
    assert resp.status_code == 200


def test_invite_page_redirects_unauthenticated(client):
    resp = client.get("/portal/admin/invite")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"
