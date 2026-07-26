"""Authentification du dashboard, indépendante des routes métier."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from flask import Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


class DashboardAuthenticator:
    def __init__(
        self,
        token_provider: Callable[[], str | None],
        *,
        cookie_name: str = "tandem_session",
        max_age: int = 12 * 3600,
        salt: str = "btcquant-dashboard-session-v1",
    ) -> None:
        self.token_provider = token_provider
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.salt = salt

    @property
    def configured(self) -> bool:
        return bool(self.token_provider())

    def verify_token(self, supplied: str) -> bool:
        expected = self.token_provider()
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def _serializer(self) -> URLSafeTimedSerializer | None:
        token = self.token_provider()
        return URLSafeTimedSerializer(token, salt=self.salt) if token else None

    def session_value(self) -> str:
        serializer = self._serializer()
        if serializer is None:
            raise RuntimeError("Authentification dashboard non configurée")
        return serializer.dumps({"scope": "dashboard"})

    def valid_session(self, value: str) -> bool:
        serializer = self._serializer()
        if serializer is None or not value:
            return False
        try:
            payload = serializer.loads(value, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return False
        return payload == {"scope": "dashboard"}

    def set_session_cookie(self, response: Response, *, secure: bool) -> None:
        response.set_cookie(
            self.cookie_name,
            self.session_value(),
            max_age=self.max_age,
            httponly=True,
            samesite="Strict",
            secure=secure,
        )
