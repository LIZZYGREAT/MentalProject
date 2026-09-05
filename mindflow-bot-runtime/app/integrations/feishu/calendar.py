"""Calendar access always resolves the participant's own encrypted token."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Awaitable, Callable
from urllib.parse import quote
import uuid

import httpx

from app.services.token_service import TokenRefreshService


_RECURRENCE_FREQUENCIES = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
_RECURRENCE_WEEKDAYS = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}


class CalendarProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_code: Any = None,
        request_kind: str,
    ) -> None:
        self.status_code = status_code
        self.provider_code = provider_code
        self.request_kind = request_kind
        super().__init__(message)


class CalendarMutationRejected(CalendarProviderError):
    """The provider definitively rejected a Calendar mutation."""


class CalendarMutationNotSent(CalendarProviderError):
    """The Calendar mutation request was never dispatched."""


class CalendarMutationOutcomeUnknown(CalendarProviderError):
    """A mutation was sent, but its committed result is not known locally."""


class CalendarProviderUnavailable(CalendarProviderError):
    """A non-mutating provider prerequisite/read was unavailable."""


def build_recurrence_rule(
    frequency: str | None,
    *,
    interval: int = 1,
    weekdays: list[str] | None = None,
    count: int | None = None,
    until: datetime | None = None,
) -> str | None:
    """Build the small reviewed RFC5545 subset exposed to the Agent."""

    if frequency is None:
        return None
    normalized_frequency = str(frequency).strip().upper()
    if normalized_frequency not in _RECURRENCE_FREQUENCIES:
        raise ValueError("unsupported calendar recurrence frequency")
    normalized_interval = int(interval)
    if not 1 <= normalized_interval <= 99:
        raise ValueError("calendar recurrence interval must be between 1 and 99")
    if count is not None and until is not None:
        raise ValueError("calendar recurrence cannot use count and until together")

    parts = [f"FREQ={normalized_frequency}", f"INTERVAL={normalized_interval}"]
    normalized_weekdays = [str(value).strip().upper() for value in (weekdays or [])]
    if normalized_weekdays:
        if normalized_frequency != "WEEKLY":
            raise ValueError("calendar recurrence weekdays are only valid for weekly events")
        if len(set(normalized_weekdays)) != len(normalized_weekdays):
            raise ValueError("calendar recurrence weekdays must be unique")
        if any(value not in _RECURRENCE_WEEKDAYS for value in normalized_weekdays):
            raise ValueError("calendar recurrence contains an invalid weekday")
        parts.append("BYDAY=" + ",".join(normalized_weekdays))
    if count is not None:
        normalized_count = int(count)
        if not 1 <= normalized_count <= 999:
            raise ValueError("calendar recurrence count must be between 1 and 999")
        parts.append(f"COUNT={normalized_count}")
    if until is not None:
        if until.tzinfo is None:
            raise ValueError("calendar recurrence until must include a timezone")
        parts.append(until.astimezone(timezone.utc).strftime("UNTIL=%Y%m%dT%H%M%SZ"))
    return ";".join(parts)


def _validate_recurrence_rule(value: str) -> str:
    """Validate the exact backend-generated recurrence subset before dispatch."""

    normalized = str(value or "").strip().upper()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise ValueError("recurring calendar event requires a recurrence rule")
    parts: dict[str, str] = {}
    for component in normalized.split(";"):
        key, separator, item = component.partition("=")
        if not separator or not key or not item or key in parts:
            raise ValueError("calendar recurrence rule is invalid")
        parts[key] = item
    if set(parts) - {"FREQ", "INTERVAL", "BYDAY", "COUNT", "UNTIL"}:
        raise ValueError("calendar recurrence rule contains unsupported fields")
    frequency = parts.get("FREQ")
    if frequency not in _RECURRENCE_FREQUENCIES:
        raise ValueError("unsupported calendar recurrence frequency")
    try:
        interval = int(parts.get("INTERVAL", "1"))
    except ValueError as exc:
        raise ValueError("calendar recurrence interval is invalid") from exc
    if not 1 <= interval <= 99:
        raise ValueError("calendar recurrence interval must be between 1 and 99")
    weekdays = parts.get("BYDAY", "").split(",") if parts.get("BYDAY") else []
    if weekdays:
        if frequency != "WEEKLY" or len(set(weekdays)) != len(weekdays):
            raise ValueError("calendar recurrence weekdays are invalid")
        if any(day not in _RECURRENCE_WEEKDAYS for day in weekdays):
            raise ValueError("calendar recurrence contains an invalid weekday")
    if "COUNT" in parts and "UNTIL" in parts:
        raise ValueError("calendar recurrence cannot use count and until together")
    if "COUNT" in parts:
        try:
            count = int(parts["COUNT"])
        except ValueError as exc:
            raise ValueError("calendar recurrence count is invalid") from exc
        if not 1 <= count <= 999:
            raise ValueError("calendar recurrence count must be between 1 and 999")
    if "UNTIL" in parts:
        try:
            datetime.strptime(parts["UNTIL"], "%Y%m%dT%H%M%SZ")
        except ValueError as exc:
            raise ValueError("calendar recurrence until is invalid") from exc
    rebuilt = [f"FREQ={frequency}", f"INTERVAL={interval}"]
    if weekdays:
        rebuilt.append("BYDAY=" + ",".join(weekdays))
    if "COUNT" in parts:
        rebuilt.append(f"COUNT={int(parts['COUNT'])}")
    if "UNTIL" in parts:
        rebuilt.append("UNTIL=" + parts["UNTIL"])
    return ";".join(rebuilt)


class CalendarService:
    def __init__(
        self, tokens: TokenRefreshService, *, timeout_seconds: float = 12.0,
        timezone_name: str = "Asia/Shanghai",
    ):
        self.tokens = tokens
        self.timeout_seconds = timeout_seconds
        self.timezone_name = timezone_name

    async def list_calendars(
        self, participant_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        token = await self.tokens.get_access_token(participant_id)
        headers = {"Authorization": f"Bearer {token}"}
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for _page in range(10):
                params = {"page_size": "100"}
                if page_token:
                    params["page_token"] = page_token
                response = await client.get(
                    "https://open.feishu.cn/open-apis/calendar/v4/calendars",
                    headers=headers,
                    params=params,
                )
                data = self._checked(response).get("data") or {}
                items.extend(data.get("calendar_list") or data.get("items") or [])
                if not data.get("has_more"):
                    break
                page_token = str(data.get("page_token") or "")
                if not page_token:
                    raise RuntimeError("Feishu calendar pagination token is missing")
            else:
                raise RuntimeError("Feishu calendar pagination exceeded the safety limit")
        return [self._sanitize_calendar(item) for item in items]

    async def get_events(
        self, participant_id: uuid.UUID, start_time: datetime, end_time: datetime
    ) -> list[dict[str, Any]]:
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError("calendar times must include a timezone")
        if end_time <= start_time or end_time - start_time > timedelta(days=31):
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

    async def create_event(
        self,
        participant_id: uuid.UUID,
        *,
        summary: str,
        start_time: datetime,
        end_time: datetime,
        description: str = "",
        reminder_minutes: int | None = None,
        recurrence: str | None = None,
        source_message_id: str,
    ) -> dict[str, Any]:
        """Compatibility wrapper; new callers use the explicit methods below."""

        if recurrence:
            return await self.create_recurring_event(
                participant_id,
                summary=summary,
                start_time=start_time,
                end_time=end_time,
                description=description,
                reminder_minutes=reminder_minutes,
                recurrence=recurrence,
                source_message_id=source_message_id,
            )
        return await self.create_single_event(
            participant_id,
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            description=description,
            reminder_minutes=reminder_minutes,
            source_message_id=source_message_id,
        )

    async def create_single_event(
        self,
        participant_id: uuid.UUID,
        *,
        summary: str,
        start_time: datetime,
        end_time: datetime,
        description: str = "",
        reminder_minutes: int | None = None,
        source_message_id: str,
    ) -> dict[str, Any]:
        return await self._create_event(
            participant_id,
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            description=description,
            reminder_minutes=reminder_minutes,
            recurrence=None,
            source_message_id=source_message_id,
        )

    async def create_recurring_event(
        self,
        participant_id: uuid.UUID,
        *,
        summary: str,
        start_time: datetime,
        end_time: datetime,
        recurrence: str,
        description: str = "",
        reminder_minutes: int | None = None,
        source_message_id: str,
    ) -> dict[str, Any]:
        normalized_recurrence = _validate_recurrence_rule(recurrence)
        return await self._create_event(
            participant_id,
            summary=summary,
            start_time=start_time,
            end_time=end_time,
            description=description,
            reminder_minutes=reminder_minutes,
            recurrence=normalized_recurrence,
            source_message_id=source_message_id,
        )

    async def _create_event(
        self,
        participant_id: uuid.UUID,
        *,
        summary: str,
        start_time: datetime,
        end_time: datetime,
        description: str,
        reminder_minutes: int | None,
        recurrence: str | None,
        source_message_id: str,
    ) -> dict[str, Any]:
        title = str(summary).strip()
        if not title or len(title) > 200:
            raise ValueError("calendar event summary must be 1-200 characters")
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError("calendar times must include a timezone")
        if end_time <= start_time or end_time - start_time > timedelta(days=31):
            raise ValueError("calendar event range must be positive and no longer than 31 days")
        if reminder_minutes is not None and not 0 <= int(reminder_minutes) <= 1440:
            raise ValueError("calendar reminder must be between 0 and 1440 minutes")

        payload: dict[str, Any] = {
            "summary": title,
            "description": str(description)[:5000],
            "start_time": {
                "timestamp": str(int(start_time.timestamp())),
                "timezone": self.timezone_name,
            },
            "end_time": {
                "timestamp": str(int(end_time.timestamp())),
                "timezone": self.timezone_name,
            },
        }
        if reminder_minutes is not None:
            payload["reminders"] = [{"minutes": int(reminder_minutes)}]
        if recurrence:
            payload["recurrence"] = str(recurrence)
        idempotency_key = hashlib.sha256(
            (
                f"{participant_id}\0{source_message_id}\0{title}\0"
                f"{start_time.isoformat()}\0{end_time.isoformat()}\0{recurrence or ''}"
            ).encode("utf-8")
        ).hexdigest()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            headers, calendar_id = await self._mutation_preflight(
                participant_id, client
            )
            data = await self._dispatch_mutation(
                "create_event",
                lambda: client.post(
                    "https://open.feishu.cn/open-apis/calendar/v4/calendars/"
                    + quote(calendar_id, safe="")
                    + "/events",
                    headers=headers,
                    params={"idempotency_key": idempotency_key},
                    json=payload,
                ),
            )
        payload_data = data.get("data") or {}
        event = payload_data.get("event") or payload_data
        return self._sanitize_event(event)

    async def get_event(
        self, participant_id: uuid.UUID, event_id: str
    ) -> dict[str, Any]:
        normalized_event_id = str(event_id).strip()
        if not normalized_event_id or len(normalized_event_id) > 256:
            raise ValueError("calendar event id is invalid")
        token = await self.tokens.get_access_token(participant_id)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            primary_response = await client.post(
                "https://open.feishu.cn/open-apis/calendar/v4/calendars/primary",
                headers=headers,
                params={"user_id_type": "open_id"},
            )
            calendar_id = self._calendar_id(self._checked(primary_response))
            response = await client.get(
                "https://open.feishu.cn/open-apis/calendar/v4/calendars/"
                + quote(calendar_id, safe="")
                + "/events/"
                + quote(normalized_event_id, safe=""),
                headers=headers,
            )
        data = self._checked(
            response, request_kind="get_event", mutation=False
        ).get("data") or {}
        return self._sanitize_event(data.get("event") or data)

    async def update_event(
        self,
        participant_id: uuid.UUID,
        event_id: str,
        *,
        summary: str | None = None,
        description: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        reminder_minutes: int | None = None,
        recurrence: str | None = None,
        clear_recurrence: bool = False,
    ) -> dict[str, Any]:
        normalized_event_id = str(event_id).strip()
        if not normalized_event_id or len(normalized_event_id) > 256:
            raise ValueError("calendar event id is invalid")
        if (start_time is None) != (end_time is None):
            raise ValueError("calendar event start and end must be updated together")
        if start_time is not None and end_time is not None:
            if start_time.tzinfo is None or end_time.tzinfo is None:
                raise ValueError("calendar times must include a timezone")
            if end_time <= start_time or end_time - start_time > timedelta(days=31):
                raise ValueError("calendar event range must be positive and no longer than 31 days")
        if summary is not None and not 1 <= len(str(summary).strip()) <= 200:
            raise ValueError("calendar event summary must be 1-200 characters")
        if reminder_minutes is not None and not 0 <= int(reminder_minutes) <= 1440:
            raise ValueError("calendar reminder must be between 0 and 1440 minutes")
        if recurrence and clear_recurrence:
            raise ValueError("calendar recurrence cannot be set and cleared together")

        payload: dict[str, Any] = {}
        if summary is not None:
            payload["summary"] = str(summary).strip()
        if description is not None:
            payload["description"] = str(description)[:5000]
        if start_time is not None and end_time is not None:
            payload["start_time"] = {
                "timestamp": str(int(start_time.timestamp())),
                "timezone": self.timezone_name,
            }
            payload["end_time"] = {
                "timestamp": str(int(end_time.timestamp())),
                "timezone": self.timezone_name,
            }
        if reminder_minutes is not None:
            payload["reminders"] = [{"minutes": int(reminder_minutes)}]
        if recurrence:
            payload["recurrence"] = str(recurrence)
        elif clear_recurrence:
            payload["recurrence"] = ""
        if not payload:
            raise ValueError("calendar event update has no changes")

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            headers, calendar_id = await self._mutation_preflight(
                participant_id, client
            )
            data = await self._dispatch_mutation(
                "update_event",
                lambda: client.patch(
                    "https://open.feishu.cn/open-apis/calendar/v4/calendars/"
                    + quote(calendar_id, safe="")
                    + "/events/"
                    + quote(normalized_event_id, safe=""),
                    headers=headers,
                    json=payload,
                ),
            )
        payload_data = data.get("data") or {}
        return self._sanitize_event(payload_data.get("event") or payload_data)

    async def delete_event(
        self, participant_id: uuid.UUID, event_id: str
    ) -> dict[str, Any]:
        normalized_event_id = str(event_id).strip()
        if not normalized_event_id or len(normalized_event_id) > 256:
            raise ValueError("calendar event id is invalid")
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            headers, calendar_id = await self._mutation_preflight(
                participant_id, client
            )
            await self._dispatch_mutation(
                "delete_event",
                lambda: client.delete(
                    "https://open.feishu.cn/open-apis/calendar/v4/calendars/"
                    + quote(calendar_id, safe="")
                    + "/events/"
                    + quote(normalized_event_id, safe=""),
                    headers=headers,
                ),
            )
        return {"id": normalized_event_id, "deleted": True}

    async def _mutation_preflight(
        self, participant_id: uuid.UUID, client: httpx.AsyncClient
    ) -> tuple[dict[str, str], str]:
        """Resolve auth and primary Calendar before crossing the write boundary."""

        try:
            token = await self.tokens.get_access_token(participant_id)
            headers = {"Authorization": f"Bearer {token}"}
            primary_response = await client.post(
                "https://open.feishu.cn/open-apis/calendar/v4/calendars/primary",
                headers=headers,
                params={"user_id_type": "open_id"},
            )
            payload = self._checked(
                primary_response,
                request_kind="primary_calendar_lookup",
                mutation=False,
            )
            return headers, self._calendar_id(payload)
        except PermissionError:
            raise
        except CalendarProviderError as exc:
            raise CalendarMutationNotSent(
                "Calendar mutation preflight failed before dispatch",
                status_code=exc.status_code,
                provider_code=exc.provider_code,
                request_kind=exc.request_kind,
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CalendarMutationNotSent(
                "Calendar mutation preflight failed before dispatch",
                request_kind="primary_calendar_lookup",
            ) from exc

    async def _dispatch_mutation(
        self,
        request_kind: str,
        send: Callable[[], Awaitable[httpx.Response]],
    ) -> dict[str, Any]:
        """Dispatch one write; transport failures beyond this boundary are unknown."""

        try:
            response = await send()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CalendarMutationOutcomeUnknown(
                "Feishu calendar mutation outcome is unknown",
                request_kind=request_kind,
            ) from exc
        return self._checked(response, request_kind=request_kind, mutation=True)

    @staticmethod
    def _checked(
        response: httpx.Response,
        *,
        request_kind: str = "calendar_read",
        mutation: bool = False,
    ) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or data.get("code") not in (None, 0):
            values = {
                "status_code": response.status_code,
                "provider_code": data.get("code"),
                "request_kind": request_kind,
            }
            if mutation and response.status_code >= 500:
                raise CalendarMutationOutcomeUnknown(
                    "Feishu calendar mutation outcome is unknown", **values
                )
            if response.status_code >= 500:
                raise CalendarProviderUnavailable(
                    "Feishu calendar provider is unavailable", **values
                )
            raise CalendarMutationRejected(
                "Feishu calendar request was rejected", **values
            )
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
    def _sanitize_calendar(item: dict[str, Any]) -> dict[str, Any]:
        calendar = item.get("calendar") if isinstance(item.get("calendar"), dict) else item
        return {
            "summary": str(calendar.get("summary") or "")[:200],
            "description": str(calendar.get("description") or "")[:500],
            "type": str(calendar.get("type") or ""),
            "role": str(calendar.get("role") or ""),
            "is_deleted": bool(calendar.get("is_deleted")),
        }

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
            "recurrence": str(item.get("recurrence") or ""),
            "status": str(item.get("status") or ""),
            "is_exception": bool(item.get("is_exception")),
            "recurring_event_id": str(item.get("recurring_event_id") or ""),
        }
