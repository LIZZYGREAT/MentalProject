"""Participant-level Feishu OAuth device flow and token refresh."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid
from typing import Any

import httpx
from sqlalchemy import select

from app.db import Database
from app.models import FeishuDeviceFlow
from app.services.token_service import (
    OAuthTokenSet,
    TokenEncryptionService,
    TokenRepository,
)


class FeishuOAuthError(RuntimeError):
    def __init__(self, message: str, *, code: Any = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class FeishuOAuthClient:
    def __init__(self, app_id: str, app_secret: str, *, timeout_seconds: float = 15.0):
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout_seconds = timeout_seconds

    async def start_device_flow(self, scope: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                "https://accounts.feishu.cn/oauth/v1/device_authorization",
                data={
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "scope": scope,
                },
            )
        return self._checked(response)

    async def poll_device_token(self, device_code: str) -> OAuthTokenSet:
        return await self._request_token(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "device_code": device_code,
            },
            fallback_refresh_token=None,
        )

    async def refresh_token(self, refresh_token: str) -> OAuthTokenSet:
        return await self._request_token(
            {
                "grant_type": "refresh_token",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "refresh_token": refresh_token,
            },
            fallback_refresh_token=refresh_token,
        )

    async def _request_token(
        self,
        form: dict[str, str],
        *,
        fallback_refresh_token: str | None,
    ) -> OAuthTokenSet:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                "https://open.feishu.cn/open-apis/authen/v2/oauth/token", data=form
            )
        data = self._checked(response)
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        now = datetime.now(timezone.utc)
        effective_refresh = payload.get("refresh_token") or fallback_refresh_token
        if not payload.get("access_token") or not effective_refresh:
            raise FeishuOAuthError("OAuth response is missing tokens")
        scopes = payload.get("scope")
        return OAuthTokenSet(
            access_token=str(payload["access_token"]),
            refresh_token=str(effective_refresh),
            access_token_expires_at=now
            + timedelta(seconds=int(payload.get("expires_in") or 7200)),
            refresh_token_expires_at=now
            + timedelta(seconds=int(payload.get("refresh_token_expires_in") or 0))
            if payload.get("refresh_token_expires_in")
            else None,
            granted_scopes=str(scopes).split() if scopes else None,
        )

    @staticmethod
    def _checked(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuOAuthError("Feishu OAuth returned invalid JSON", retryable=True) from exc
        error = payload.get("error")
        code = payload.get("code")
        if response.status_code >= 500:
            raise FeishuOAuthError("Feishu OAuth is temporarily unavailable", code=code, retryable=True)
        if response.status_code >= 400 or error or code not in (None, 0):
            actual = error or code
            retryable = actual in {"authorization_pending", "slow_down", 91031, 91032}
            raise FeishuOAuthError(
                str(payload.get("error_description") or payload.get("msg") or actual),
                code=actual,
                retryable=retryable,
            )
        return payload


class DeviceFlowService:
    DEFAULT_SCOPE = "offline_access calendar:calendar:readonly"

    def __init__(
        self,
        database: Database,
        encryption: TokenEncryptionService,
        tokens: TokenRepository,
        oauth: FeishuOAuthClient,
    ):
        self.database = database
        self.encryption = encryption
        self.tokens = tokens
        self.oauth = oauth

    def pending_participants(self) -> list[uuid.UUID]:
        with self.database.session() as session:
            return list(
                session.execute(
                    select(FeishuDeviceFlow.participant_id).where(
                        FeishuDeviceFlow.status == "pending",
                        FeishuDeviceFlow.expires_at > datetime.now(timezone.utc),
                    )
                ).scalars()
            )

    async def start(self, participant_id: uuid.UUID) -> dict[str, Any]:
        result = await self.oauth.start_device_flow(self.DEFAULT_SCOPE)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=int(result.get("expires_in") or 300))
        verification_url = str(
            result.get("verification_uri_complete") or result.get("verification_uri") or ""
        )
        with self.database.session() as session:
            row = session.get(FeishuDeviceFlow, participant_id, with_for_update=True)
            values = {
                "device_code_ciphertext": self.encryption.encrypt(
                    str(result["device_code"]),
                    participant_id=participant_id,
                    purpose="device_flow",
                ),
                "user_code": str(result["user_code"]),
                "verification_url": verification_url,
                "interval_seconds": int(result.get("interval") or 5),
                "expires_at": expires_at,
                "status": "pending",
                "updated_at": now,
            }
            if row is None:
                session.add(FeishuDeviceFlow(participant_id=participant_id, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
        return {
            "user_code": str(result["user_code"]),
            "verification_url": verification_url,
            "expires_at": expires_at.isoformat(),
        }

    async def poll_until_complete(self, participant_id: uuid.UUID) -> None:
        while True:
            with self.database.session() as session:
                row = session.get(FeishuDeviceFlow, participant_id)
                if row is None or row.status != "pending":
                    return
                expires_at = row.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    row.status = "expired"
                    return
                interval = row.interval_seconds
                device_code = self.encryption.decrypt(
                    row.device_code_ciphertext,
                    participant_id=participant_id,
                    purpose="device_flow",
                )
            await asyncio.sleep(max(1, interval))
            try:
                tokens = await self.oauth.poll_device_token(device_code)
            except FeishuOAuthError as exc:
                if exc.code in {"authorization_pending", 91031}:
                    continue
                if exc.code in {"slow_down", 91032}:
                    await asyncio.sleep(5)
                    continue
                with self.database.session() as session:
                    row = session.get(FeishuDeviceFlow, participant_id, with_for_update=True)
                    if row is not None and row.status == "pending":
                        row.status = "failed"
                raise
            self.tokens.save(participant_id, tokens)
            with self.database.session() as session:
                row = session.get(FeishuDeviceFlow, participant_id, with_for_update=True)
                if row is not None:
                    row.status = "complete"
            return
