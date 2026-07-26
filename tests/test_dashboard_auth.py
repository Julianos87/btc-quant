"""Authentification du dashboard sans secret dans l'URL."""

from __future__ import annotations

import dashboard.app as dashboard


def test_health_probe_never_exposes_business_data(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", "very-secret-token")

    response = dashboard.app.test_client().get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_url_token_is_rejected_and_redirects_to_login(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", "very-secret-token")
    client = dashboard.app.test_client()

    response = client.get("/?k=very-secret-token")

    assert response.status_code == 303
    assert response.headers["Location"] == "/login"
    assert "Set-Cookie" not in response.headers


def test_login_issues_short_signed_secure_session(monkeypatch):
    token = "very-secret-token"
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", token)
    client = dashboard.app.test_client()

    response = client.post(
        "/login",
        data={"token": token},
        headers={"X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 303
    cookie = response.headers["Set-Cookie"]
    assert dashboard.COOKIE_NAME in cookie
    assert token not in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Strict" in cookie
    assert f"Max-Age={dashboard.COOKIE_MAX_AGE}" in cookie


def test_invalid_login_does_not_create_session(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", "very-secret-token")
    response = dashboard.app.test_client().post(
        "/login",
        data={"token": "wrong"},
    )

    assert response.status_code == 403
    assert "Set-Cookie" not in response.headers


def test_manifest_never_contains_authentication_secret(monkeypatch):
    token = "very-secret-token"
    monkeypatch.setattr(dashboard, "AUTH_TOKEN", token)
    client = dashboard.app.test_client()
    client.post("/login", data={"token": token})

    response = client.get("/manifest.json")

    assert response.status_code == 200
    assert response.get_json()["start_url"] == "/"
    assert token.encode() not in response.data
    assert response.headers["Cache-Control"] == "no-store"
