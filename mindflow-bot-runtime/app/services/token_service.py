"""Participant-scoped encrypted OAuth token storage and serialized refresh."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import uuid
from typing import Any, Awaitable, Callable, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select

from app.db import Database
from app.models import FeishuOAuthToken


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass(frozen=True)
class OAuthTokenSet:
    access_token: str
    refresh_token: str
    access_token_expires_at: datetime
    refresh_token_expires_at: Optional[datetime] = None
    granted_scopes: Optional[list[str]] = None


class TokenEncryptionService:
    """AES-256-GCM with participant- and token-type-bound AAD."""

    def __init__(self, encoded_key: str):
        value = str(encoded_key or "").removeprefix("base64:")
        try:
            key = _b64decode(value)
        except Exception as exc:
            raise ValueError("TOKEN_ENCRYPTION_KEY must be URL-safe base64") from exc
        if len(key) != 32:
            raise ValueError("TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes")
        self._cipher = AESGCM(key)

    @staticmethod
    def generate_key() -> str:
        return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode().rstrip("=")

    def encrypt(self, plaintext: str, *, participant_id: uuid.UUID, purpose: str) -> str:
        nonce = os.urandom(12)
        aad = f"mindflow:v1:{participant_id}:{purpose}".encode()
        ciphertext = self._cipher.encrypt(nonce, str(plaintext).encode(), aad)
        return "v1." + base64.urlsafe_b64encode(nonce).decode().rstrip("=") + "." + base64.urlsafe_b64encode(ciphertext).decode().rstrip("=")

    def decrypt(self, envelope: str, *, participant_id: uuid.UUID, purpose: str) -> str:
        try:
            version, nonce_text, ciphertext_text = str(envelope).split(".", 2)
            if version != "v1":
                raise ValueError("unsupported token envelope")
            aad = f"mindflow:v1:{participant_id}:{purpose}".encode()
            plaintext = self._cipher.decrypt(
                _b64decode(nonce_text), _b64decode(ciphertext_text), aad
            )
            return plaintext.decode()
        except Exception as exc:
            raise ValueError("token ciphertext failed authentication") from exc


class TokenRepository:
    def __init__(
        self,
        database: Database,
        encryption: TokenEncryptionService,
        *,
        oauth_app_id: str,
    ):
        self.database = database
        self.encryption = encryption
        self.oauth_app_id = oauth_app_id

    def save(self, participant_id: uuid.UUID, tokens: OAuthTokenSet) -> None:
        with self.database.session() as session:
            row = session.get(FeishuOAuthToken, participant_id, with_for_update=True)
            access = self.encryption.encrypt(
                tokens.access_token, participant_id=participant_id, purpose="access"
            )
            refresh = self.encryption.encrypt(
                tokens.refresh_token, participant_id=participant_id, purpose="refresh"
            )
            if row is None:
                row = FeishuOAuthToken(
                    participant_id=participant_id,
                    oauth_app_id=self.oauth_app_id,
                    access_token_ciphertext=access,
                    refresh_token_ciphertext=refresh,
                    access_token_expires_at=tokens.access_token_expires_at,
                    refresh_token_expires_at=tokens.refresh_token_expires_at,
                    granted_scopes=tokens.granted_scopes,
                )
                session.add(row)
            else:
                row.oauth_app_id = self.oauth_app_id
                row.access_token_ciphertext = access
                row.refresh_token_ciphertext = refresh
                row.access_token_expires_at = tokens.access_token_expires_at
                row.refresh_token_expires_at = tokens.refresh_token_expires_at
                row.granted_scopes = tokens.granted_scopes
                row.token_version += 1
                row.refresh_lease_token = None
                row.refresh_lease_until = None
                row.refresh_started_at = None
                row.updated_at = datetime.now(timezone.utc)

    def status(self, participant_id: uuid.UUID) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(FeishuOAuthToken, participant_id)
            if row is None:
                return {"connected": False, "status": "disconnected"}
            if row.oauth_app_id != self.oauth_app_id:
                return {"connected": False, "status": "reconnect_required"}
            now = datetime.now(timezone.utc)
            return {
                "connected": True,
                "status": "connected" if _aware(row.access_token_expires_at) > now else "refresh_required",
                "access_token_expires_at": _aware(row.access_token_expires_at).isoformat(),
                "scopes": list(row.granted_scopes or []),
                "token_version": row.token_version,
            }


RefreshCallable = Callable[[str], Awaitable[OAuthTokenSet]]


class TokenRefreshService:
    """Serialize refresh with short DB leases and no transaction across HTTP."""

    def __init__(
        self,
        database: Database,
        encryption: TokenEncryptionService,
        refresh: RefreshCallable,
        *,
        expected_oauth_app_id: str,
        refresh_margin_seconds: int = 300,
        refresh_lease_seconds: int = 30,
        refresh_poll_seconds: float = 0.05,
    ):
        self.database = database
        self.encryption = encryption
        self.refresh = refresh
        self.expected_oauth_app_id = expected_oauth_app_id
        self.refresh_margin = timedelta(seconds=max(0, refresh_margin_seconds))
        self.refresh_lease = timedelta(seconds=max(5, refresh_lease_seconds))
        self.refresh_poll_seconds = max(0.01, float(refresh_poll_seconds))
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    async def get_access_token(self, participant_id: uuid.UUID) -> str:
        lock = self._locks.setdefault(participant_id, asyncio.Lock())
        async with lock:
            while True:
                claim = await asyncio.to_thread(self._claim_refresh, participant_id)
                if claim["state"] == "ready":
                    return str(claim["access_token"])
                if claim["state"] == "waiting":
                    await asyncio.sleep(self.refresh_poll_seconds)
                    continue

                lease_token = str(claim["lease_token"])
                expected_version = int(claim["token_version"])
                try:
                    tokens = await self.refresh(str(claim["refresh_token"]))
                except BaseException:
                    await asyncio.to_thread(
                        self._release_refresh_lease,
                        participant_id,
                        lease_token,
                    )
                    raise
                access = await asyncio.to_thread(
                    self._finalize_refresh,
                    participant_id,
                    lease_token,
                    expected_version,
                    tokens,
                )
                if access is not None:
                    return access

    def _claim_refresh(self, participant_id: uuid.UUID) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self.database.session() as session:
            row = session.execute(
                select(FeishuOAuthToken)
                .where(FeishuOAuthToken.participant_id == participant_id)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise PermissionError("calendar is not connected")
            if row.oauth_app_id != self.expected_oauth_app_id:
                raise PermissionError(
                    "calendar authorization belongs to another Feishu app; "
                    "reconnect required"
                )
            if _aware(row.access_token_expires_at) > now + self.refresh_margin:
                return {
                    "state": "ready",
                    "access_token": self.encryption.decrypt(
                        row.access_token_ciphertext,
                        participant_id=participant_id,
                        purpose="access",
                    ),
                }
            if row.refresh_token_expires_at and _aware(row.refresh_token_expires_at) <= now:
                raise PermissionError("calendar authorization has expired")
            if (
                row.refresh_lease_token
                and row.refresh_lease_until
                and _aware(row.refresh_lease_until) > now
            ):
                return {"state": "waiting"}
            lease_token = uuid.uuid4().hex
            row.refresh_lease_token = lease_token
            row.refresh_lease_until = now + self.refresh_lease
            row.refresh_started_at = now
            row.updated_at = now
            return {
                "state": "claimed",
                "lease_token": lease_token,
                "token_version": row.token_version,
                "refresh_token": self.encryption.decrypt(
                    row.refresh_token_ciphertext,
                    participant_id=participant_id,
                    purpose="refresh",
                ),
            }

    def _release_refresh_lease(
        self, participant_id: uuid.UUID, lease_token: str
    ) -> None:
        with self.database.session() as session:
            row = session.get(FeishuOAuthToken, participant_id, with_for_update=True)
            if row is not None and row.refresh_lease_token == lease_token:
                row.refresh_lease_token = None
                row.refresh_lease_until = None
                row.refresh_started_at = None
                row.updated_at = datetime.now(timezone.utc)

    def _finalize_refresh(
        self,
        participant_id: uuid.UUID,
        lease_token: str,
        expected_version: int,
        tokens: OAuthTokenSet,
    ) -> str | None:
        now = datetime.now(timezone.utc)
        with self.database.session() as session:
            row = session.get(FeishuOAuthToken, participant_id, with_for_update=True)
            if row is None:
                raise PermissionError("calendar is not connected")
            if (
                row.refresh_lease_token != lease_token
                or row.token_version != expected_version
            ):
                if _aware(row.access_token_expires_at) > now + self.refresh_margin:
                    return self.encryption.decrypt(
                        row.access_token_ciphertext,
                        participant_id=participant_id,
                        purpose="access",
                    )
                return None
            row.access_token_ciphertext = self.encryption.encrypt(
                tokens.access_token, participant_id=participant_id, purpose="access"
            )
            row.refresh_token_ciphertext = self.encryption.encrypt(
                tokens.refresh_token, participant_id=participant_id, purpose="refresh"
            )
            row.access_token_expires_at = tokens.access_token_expires_at
            row.refresh_token_expires_at = tokens.refresh_token_expires_at
            row.granted_scopes = tokens.granted_scopes
            row.token_version += 1
            row.refresh_lease_token = None
            row.refresh_lease_until = None
            row.refresh_started_at = None
            row.updated_at = now
            return tokens.access_token
