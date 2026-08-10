"""Calendar access always resolves the participant's own encrypted token."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
import uuid

import httpx

from app.services.token_service import TokenRefreshService


class CalendarService:
    def __init__(self, tokens: TokenRefreshService, *, timeout_seconds: float = 12.0):
        self.tokens = tokens
        self.timeout_seconds = timeout_seconds

    async def get_events(
        self, participant_id: uuid.UUID, start_time: datetime, end_time: datetime
    ) -> list[dict[str, Any]]:
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError("calendar times must include a timezone")
        if end_time <= start_time or (end_time - start_time).days > 31:
            raise ValueError("calendar range must be positive and no longer than 31 days")
        token = await self.tokens.get_access_token(participant_id)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            primary_response = await client.post(
                "https://open.feishu.cn/open-apis/calendar/v4/calendars/primary",
                headers=headers,
                params={"user_id_type": "open_id"},
            )
            primary = self._checked(primary_response)
            calendar_id = self._calendar_id(primary)
            items: list[dict[str, Any]] = []
            page_token: str | None = None
            for _page in range(10):
                params = {
                    "start_time": str(int(start_time.timestamp())),
                    "end_time": str(int(end_time.timestamp())),
                    "page_size": "100",
                }
                if page_token:
                    params["page_token"] = page_token
                events_response = await client.get(
                    "https://open.feishu.cn/open-apis/calendar/v4/calendars/"
                    + quote(calendar_id, safe="")
                    + "/events/instance_view",
                    headers=headers,
                    params=params,
                )
                payload = self._checked(events_response)
                data = payload.get("data") or {}
                items.extend(data.get("items") or [])
                if not data.get("has_more"):
                    break
                page_token = str(data.get("page_token") or "")
                if not page_token:
                    raise RuntimeError("Feishu calendar pagination token is missing")
            else:
                raise RuntimeError("Feishu calendar pagination exceeded the safety limit")
        return [self._sanitize_event(item) for item in items]

    @staticmethod
    def _checked(response: httpx.Response) -> dict[str, Any]:
        data = response.json()
        if response.status_code >= 400 or data.get("code") not in (None, 0):
            raise RuntimeError("Feishu calendar request failed")
        return data

    @staticmethod
    def _calendar_id(payload: dict[str, Any]) -> str:
        data = payload.get("data") or {}
        if isinstance(data.get("calendar"), dict):
            calendar_id = data["calendar"].get("calendar_id")
            if calendar_id:
                return str(calendar_id)
        for item in data.get("calendars") or []:
            calendar = item.get("calendar") or item
            if calendar.get("type") == "primary" or calendar.get("role") == "owner":
                if calendar.get("calendar_id"):
                    return str(calendar["calendar_id"])
        raise RuntimeError("Feishu primary calendar was not found")

    @staticmethod
    def _sanitize_event(item: dict[str, Any]) -> dict[str, Any]:
        def time_value(value: Any) -> str | None:
            if not isinstance(value, dict):
                return None
            if value.get("timestamp"):
                return datetime.fromtimestamp(
                    int(value["timestamp"]), tz=timezone.utc
                ).isoformat()
            return value.get("date")

        return {
            "id": str(item.get("event_id") or ""),
            "summary": str(item.get("summary") or "")[:200],
            "description": str(item.get("description") or "")[:500],
            "start_time": time_value(item.get("start_time")),
            "end_time": time_value(item.get("end_time")),
        }
