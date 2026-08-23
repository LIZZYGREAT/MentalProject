"""Password hashing and signed database-admin sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request


COOKIE_NAME = "mindflow_admin_session"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, *, iterations: int = 310_000) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _unb64(salt),
            int(iterations),
        )
        return hmac.compare_digest(_b64(derived), expected)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class AdminSession:
    username: str
    expires_at: int
    csrf_token: str
    user_id: str = ""
    role: str = "viewer"


class SessionSigner:
    def __init__(self, secret: str, ttl_seconds: int):
        self.secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    def issue(
        self, username: str, *, user_id: str = "", role: str = "viewer"
    ) -> tuple[str, AdminSession]:
        session = AdminSession(
            username=username,
            expires_at=int(time.time()) + self.ttl_seconds,
            csrf_token=secrets.token_urlsafe(24),
            user_id=user_id,
            role=role,
        )
        payload = _b64(
            json.dumps(
                {
                    "username": session.username,
                    "expires_at": session.expires_at,
                    "csrf_token": session.csrf_token,
                    "user_id": session.user_id,
                    "role": session.role,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature = _b64(hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).digest())
        return f"{payload}.{signature}", session

    def read(self, token: str) -> AdminSession | None:
        try:
            payload, signature = token.split(".", 1)
            expected = _b64(
                hmac.new(self.secret, payload.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(signature, expected):
                return None
            value: dict[str, Any] = json.loads(_unb64(payload))
            session = AdminSession(
                username=str(value["username"]),
                expires_at=int(value["expires_at"]),
                csrf_token=str(value["csrf_token"]),
                user_id=str(value.get("user_id") or ""),
                role=str(value.get("role") or "viewer"),
            )
            return session if session.expires_at >= int(time.time()) else None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def from_request(self, request: Request) -> AdminSession | None:
        return self.read(request.cookies.get(COOKIE_NAME, ""))
