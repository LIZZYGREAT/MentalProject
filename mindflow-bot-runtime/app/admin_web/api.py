"""Starlette routes for the isolated MindFlow administrator API."""

from __future__ import annotations

from datetime import date, datetime
import hmac
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.admin_web.auth import COOKIE_NAME, AdminSession, SessionSigner, verify_password
from app.admin_web.repositories import AdminRepository
from app.config import Settings
from app.services.pressure_curve_service import (
    HistoricalForecastNotFoundError,
    PressureCurveService,
)


def _json_error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class AdminAPI:
    def __init__(
        self,
        repository: AdminRepository,
        settings: Settings,
        pressure_curves: PressureCurveService | None,
    ):
        self.repository = repository
        self.settings = settings
        self.pressure_curves = pressure_curves
        self.signer = SessionSigner(
            settings.admin_session_secret or "test-admin-session-secret",
            settings.admin_session_ttl_seconds,
        )

    def _business_context(self) -> dict[str, str]:
        return {
            "timezone": self.settings.timezone_name,
            "business_date": datetime.now(
                ZoneInfo(self.settings.timezone_name)
            ).date().isoformat(),
        }

    @staticmethod
    def _query_int(
        request: Request,
        name: str,
        default: int,
        *,
        minimum: int = 1,
        maximum: int,
    ) -> tuple[int | None, JSONResponse | None]:
        raw = request.query_params.get(name)
        try:
            value = default if raw is None else int(raw)
        except (TypeError, ValueError):
            return None, _json_error("invalid_query_parameter", 400)
        if value < minimum or value > maximum:
            return None, _json_error("invalid_query_parameter", 400)
        return value, None

    def _session(self, request: Request) -> AdminSession | None:
        session = self.signer.from_request(request)
        if session is None or session.username != self.settings.admin_username:
            return None
        return session

    def _authorized(self, request: Request, *, csrf: bool = False) -> AdminSession | None:
        session = self._session(request)
        if session is None:
            return None
        if csrf and not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf_token
        ):
            return None
        return session

    async def health(self, _request: Request) -> Response:
        try:
            return JSONResponse(self.repository.health())
        except Exception:
            return _json_error("database_unavailable", 503)

    async def login(self, request: Request) -> Response:
        try:
            value = await request.json()
        except Exception:
            return _json_error("invalid_json", 400)
        username = str(value.get("username") or "")
        password = str(value.get("password") or "")
        if not (
            hmac.compare_digest(username, self.settings.admin_username)
            and verify_password(password, self.settings.admin_password_hash)
        ):
            return _json_error("invalid_credentials", 401)
        token, session = self.signer.issue(username)
        response = JSONResponse(
            {
                "authenticated": True,
                "username": username,
                "csrf_token": session.csrf_token,
                **self._business_context(),
            }
        )
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=self.settings.admin_session_ttl_seconds,
            httponly=True,
            secure=self.settings.admin_secure_cookie,
            samesite="strict",
            path="/admin",
        )
        return response

    async def session(self, request: Request) -> Response:
        session = self._authorized(request)
        if session is None:
            return _json_error("unauthorized", 401)
        return JSONResponse(
            {
                "authenticated": True,
                "username": session.username,
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at,
                **self._business_context(),
            }
        )

    async def logout(self, request: Request) -> Response:
        if self._authorized(request, csrf=True) is None:
            return _json_error("unauthorized_or_csrf", 401)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE_NAME, path="/admin")
        return response

    async def dashboard(self, request: Request) -> Response:
        if self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        return JSONResponse(self.repository.dashboard())

    async def participants(self, request: Request) -> Response:
        if self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        query = request.query_params
        page, error = self._query_int(request, "page", 1, maximum=1_000_000)
        if error:
            return error
        limit, error = self._query_int(request, "limit", 25, maximum=100)
        if error:
            return error
        return JSONResponse(
            self.repository.participants(
                search=query.get("search", ""),
                status=query.get("status", ""),
                page=page,
                limit=limit,
            )
        )

    def _participant(self, request: Request) -> tuple[Any, Response | None]:
        if self._authorized(request) is None:
            return None, _json_error("unauthorized", 401)
        code = request.path_params["participant_code"]
        participant_id = self.repository.participant_id(code)
        if participant_id is None:
            return None, _json_error("participant_not_found", 404)
        return participant_id, None

    async def participant(self, request: Request) -> Response:
        if self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        value = self.repository.participant(request.path_params["participant_code"])
        return JSONResponse(value) if value else _json_error("participant_not_found", 404)

    async def messages(self, request: Request) -> Response:
        participant_id, error = self._participant(request)
        if error:
            return error
        query = request.query_params
        limit, query_error = self._query_int(
            request, "limit", 50, maximum=200
        )
        if query_error:
            return query_error
        items = self.repository.messages(
            participant_id,
            status=query.get("status", ""),
            error_only=query.get("error_only", "").lower() in {"1", "true", "yes"},
            limit=limit,
        )
        return JSONResponse({"items": items})

    async def message(self, request: Request) -> Response:
        if self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        value = self.repository.message(request.path_params["event_id"])
        return JSONResponse(value) if value else _json_error("message_not_found", 404)

    async def observations(self, request: Request) -> Response:
        participant_id, error = self._participant(request)
        if error:
            return error
        return JSONResponse({"items": self.repository.observations(participant_id)})

    async def calendars(self, request: Request) -> Response:
        participant_id, error = self._participant(request)
        if error:
            return error
        return JSONResponse({"items": self.repository.calendars(participant_id)})

    async def forecasts(self, request: Request) -> Response:
        participant_id, error = self._participant(request)
        if error:
            return error
        return JSONResponse({"items": self.repository.forecasts(participant_id)})

    async def forecast(self, request: Request) -> Response:
        participant_id, error = self._participant(request)
        if error:
            return error
        try:
            target = date.fromisoformat(request.path_params["local_date"])
        except ValueError:
            return _json_error("invalid_local_date", 400)
        value = self.repository.forecast(participant_id, target)
        return JSONResponse(value) if value else _json_error("forecast_not_found", 404)

    async def refresh_forecast(self, request: Request) -> Response:
        session = self._authorized(request, csrf=True)
        if session is None:
            return _json_error("unauthorized_or_csrf", 401)
        participant_id = self.repository.participant_id(
            request.path_params["participant_code"]
        )
        if participant_id is None:
            return _json_error("participant_not_found", 404)
        try:
            target = date.fromisoformat(request.path_params["local_date"])
        except ValueError:
            return _json_error("invalid_local_date", 400)
        if target < datetime.now(ZoneInfo(self.settings.timezone_name)).date():
            return _json_error("historical_forecast_refresh_not_supported", 409)
        if self.pressure_curves is None:
            return _json_error("forecast_service_unavailable", 503)
        view = await self.pressure_curves.build(
            participant_id,
            target,
            reason="admin_forecast_refresh",
            refresh_calendar=True,
            stress_only=False,
        )
        return JSONResponse(
            {
                "forecast_version": view.forecast.get("forecast_version"),
                "local_date": view.forecast.get("local_date"),
                "initial_state": (view.forecast.get("output") or {}).get("initial_state"),
                "initial_state_revision": (view.forecast.get("output") or {}).get(
                    "initial_state_revision"
                ),
                "analysis": view.analysis.to_dict(),
            }
        )

    async def curve_json(self, request: Request) -> Response:
        participant_id, error = self._participant(request)
        if error:
            return error
        try:
            target = date.fromisoformat(request.path_params["local_date"])
        except ValueError:
            return _json_error("invalid_local_date", 400)
        if self.pressure_curves is None:
            forecast = self.repository.forecast(participant_id, target)
            return JSONResponse(forecast) if forecast else _json_error("forecast_not_found", 404)
        try:
            view = await self.pressure_curves.build(
                participant_id,
                target,
                reason="admin_curve_view",
                refresh_calendar=False,
                stress_only=False,
            )
        except HistoricalForecastNotFoundError:
            return _json_error("forecast_not_found", 404)
        output = dict(view.forecast.get("output") or {})
        return JSONResponse(
            {
                "local_date": str(view.forecast.get("local_date") or target),
                "forecast_version": view.forecast.get("forecast_version"),
                "curve": list(view.forecast.get("curve") or []),
                "analysis": view.analysis.to_dict(),
                "events": list(view.forecast.get("calendar_events") or []),
                "warnings": list(view.forecast.get("warning_windows") or []),
                "initial_state": dict(output.get("initial_state") or {}),
                "initial_state_revision": output.get("initial_state_revision"),
                "calendar_degraded": bool(view.forecast.get("calendar_degraded")),
                "semantic_status": view.forecast.get("semantic_status"),
            }
        )

    async def curve_png(self, request: Request) -> Response:
        participant_id, error = self._participant(request)
        if error:
            return error
        if self.pressure_curves is None:
            return _json_error("forecast_service_unavailable", 503)
        try:
            target = date.fromisoformat(request.path_params["local_date"])
        except ValueError:
            return _json_error("invalid_local_date", 400)
        try:
            view = await self.pressure_curves.build(
                participant_id,
                target,
                reason="admin_curve_png",
                refresh_calendar=False,
                stress_only=False,
            )
        except HistoricalForecastNotFoundError:
            return _json_error("forecast_not_found", 404)
        return Response(
            view.png_bytes,
            media_type="image/png",
            headers={"Cache-Control": "private, no-store"},
        )

    async def warnings(self, request: Request) -> Response:
        participant_id, error = self._participant(request)
        if error:
            return error
        return JSONResponse({"items": self.repository.warnings(participant_id)})

    async def incidents(self, request: Request) -> Response:
        if self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        limit, error = self._query_int(request, "limit", 100, maximum=500)
        if error:
            return error
        return JSONResponse(
            {"items": self.repository.incidents(limit)}
        )

    def routes(self) -> list[Route]:
        prefix = "/admin/api"
        return [
            Route(f"{prefix}/health", self.health, methods=["GET"]),
            Route(f"{prefix}/login", self.login, methods=["POST"]),
            Route(f"{prefix}/session", self.session, methods=["GET"]),
            Route(f"{prefix}/logout", self.logout, methods=["POST"]),
            Route(f"{prefix}/dashboard", self.dashboard, methods=["GET"]),
            Route(f"{prefix}/participants", self.participants, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}", self.participant, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/messages", self.messages, methods=["GET"]),
            Route(f"{prefix}/messages/{{event_id}}", self.message, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/observations", self.observations, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/calendar", self.calendars, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/forecasts", self.forecasts, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/forecasts/{{local_date}}", self.forecast, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/forecasts/{{local_date}}/refresh", self.refresh_forecast, methods=["POST"]),
            Route(f"{prefix}/participants/{{participant_code}}/pressure-curve/{{local_date}}.png", self.curve_png, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/pressure-curve/{{local_date}}", self.curve_json, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/warnings", self.warnings, methods=["GET"]),
            Route(f"{prefix}/incidents", self.incidents, methods=["GET"]),
        ]
