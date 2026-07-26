from __future__ import annotations

from flask import Response

from dashboard.auth import DashboardAuthenticator


def test_token_rotation_revokes_existing_session():
    token = ["first-secret"]
    auth = DashboardAuthenticator(lambda: token[0])
    cookie = auth.session_value()

    assert auth.valid_session(cookie)
    token[0] = "second-secret"
    assert not auth.valid_session(cookie)


def test_tampered_or_expired_session_is_rejected():
    auth = DashboardAuthenticator(lambda: "secret")
    cookie = auth.session_value()

    assert not auth.valid_session(cookie + "tampered")
    expired = DashboardAuthenticator(lambda: "secret", max_age=-1)
    assert not expired.valid_session(cookie)


def test_cookie_policy_is_centralized():
    auth = DashboardAuthenticator(lambda: "secret", max_age=60)
    response = Response()

    auth.set_session_cookie(response, secure=True)

    cookie = response.headers["Set-Cookie"]
    assert "Max-Age=60" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Secure" in cookie
