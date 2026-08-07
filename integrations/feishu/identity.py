"""Trusted mapping between Feishu identities and Mental_project users."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from auth.database import AppDatabase


class FeishuIdentityService:
    """Create one-time binding links and resolve trusted runtime identities."""

    def __init__(
        self,
        database: AppDatabase,
        *,
        app_id: str,
        bind_base_url: str,
        token_ttl_seconds: int = 900,
    ):
        self.database = database
        self.app_id = str(app_id or "").strip()
        self.bind_base_url = str(bind_base_url or "").strip()
        self.token_ttl_seconds = max(60, min(int(token_ttl_seconds), 3600))

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(str(raw_token).encode("utf-8")).hexdigest()

    def create_binding_token(
        self,
        *,
        tenant_key: str,
        open_id: str,
        chat_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.app_id:
            raise ValueError("FEISHU_APP_ID 未配置")
        if not self.bind_base_url:
            raise ValueError("FEISHU_BIND_BASE_URL 未配置")
        if not str(open_id or "").strip():
            raise ValueError("飞书身份缺失")

        raw_token = secrets.token_urlsafe(32)
        token_id = secrets.token_hex(16)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=self.token_ttl_seconds)
        ).isoformat(timespec="seconds")
        self.database.store_bot_binding_token(
            token_id=token_id,
            token_hash=self.hash_token(raw_token),
            app_id=self.app_id,
            tenant_key=str(tenant_key or ""),
            open_id=str(open_id),
            chat_id=chat_id,
            expires_at=expires_at,
        )
        return {
            "token": raw_token,
            "expires_at": expires_at,
            "bind_url": self._binding_url(raw_token),
        }

    def confirm_binding(self, raw_token: str, authenticated_user_id: int) -> Dict[str, Any]:
        if not isinstance(raw_token, str) or len(raw_token) < 32:
            raise ValueError("绑定链接无效、已使用或已过期")
        return self.database.confirm_feishu_binding(
            token_hash=self.hash_token(raw_token),
            user_id=int(authenticated_user_id),
        )

    def resolve_binding(self, tenant_key: str, open_id: str) -> Optional[Dict[str, Any]]:
        return self.database.resolve_feishu_binding(
            app_id=self.app_id,
            tenant_key=str(tenant_key or ""),
            open_id=str(open_id),
        )

    def status_for_user(self, user_id: int) -> Dict[str, Any]:
        binding = self.database.feishu_binding_for_user(
            int(user_id),
            app_id=self.app_id or None,
        )
        if not binding:
            return {"bound": False, "status": "unbound"}
        return {
            "bound": True,
            "status": "active",
            "bound_at": binding["bound_at"],
            "open_id_masked": self.mask_identifier(binding.get("open_id")),
        }

    def revoke_binding(self, user_id: int) -> bool:
        return self.database.revoke_feishu_binding(
            int(user_id),
            app_id=self.app_id or None,
        )

    @staticmethod
    def mask_identifier(value: Optional[str]) -> Optional[str]:
        value = str(value or "")
        if not value:
            return None
        if len(value) <= 8:
            return value[:2] + "***"
        return f"{value[:4]}***{value[-4:]}"

    def _binding_url(self, raw_token: str) -> str:
        parts = urlsplit(self.bind_base_url)
        path = parts.path.rstrip("/")
        if not path.endswith("/feishu/bind"):
            path = f"{path}/feishu/bind"
        query = urlencode({"token": raw_token})
        return urlunsplit((parts.scheme, parts.netloc, path, query, ""))
