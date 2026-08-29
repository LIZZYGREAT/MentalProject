"""Starlette routes for the isolated MindFlow administrator API."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import hmac
import uuid
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.admin_web.auth import (
    COOKIE_NAME, AdminSession, SessionSigner, hash_password, verify_password,
)
from app.admin_web.admin_users import AdminUserRepository
from app.admin_web.repositories import AdminRepository
from app.config import Settings
from app.services.pressure_curve_service import (
    HistoricalForecastNotFoundError,
    PressureCurveService,
)
from app.repositories_daily_review import (
    DailyReviewResponseRepository, RetrospectiveCurveRepository,
)
from app.services.daily_review_service import DailyReviewService
from app.services.research_evaluation import ResearchEvaluationService
from app.repositories import ForecastSnapshotRepository, ObservationRepository


def _json_error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class AdminAPI:
    def __init__(
        self,
        repository: AdminRepository,
        settings: Settings,
        pressure_curves: PressureCurveService | None,
        *,
        admin_users: AdminUserRepository | None = None,
        daily_reviews: DailyReviewService | None = None,
    ):
        self.repository = repository
        self.settings = settings
        self.pressure_curves = pressure_curves
        self.admin_users = admin_users or AdminUserRepository(repository.database)
        self.daily_reviews = daily_reviews
        self.review_responses = DailyReviewResponseRepository(repository.database)
        self.retrospectives = RetrospectiveCurveRepository(repository.database)
        self.forecasts_repository = ForecastSnapshotRepository(
            repository.database
        )
        self.observations_repository = ObservationRepository(repository.database)
        self.research = ResearchEvaluationService(
            repository.database, settings.timezone_name
        )
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

    def _research_dates(self, request: Request, default_days: int = 14):
        today = datetime.now(ZoneInfo(self.settings.timezone_name)).date()
        try:
            end = date.fromisoformat(request.query_params.get("date_end") or today.isoformat())
            start = date.fromisoformat(
                request.query_params.get("date_start")
                or (end - timedelta(days=default_days - 1)).isoformat()
            )
            if start > end or (end - start).days > 365:
                raise ValueError
            return start, end, None
        except ValueError:
            return None, None, _json_error("invalid_date_range", 400)

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

    def _session_sync(self, request: Request) -> AdminSession | None:
        session = self.signer.from_request(request)
        if session is None:
            return None
        row = self.admin_users.get(session.user_id) if session.user_id else None
        if row is None:
            row = self.admin_users.get_by_username(session.username)
        if row is None or row.status != "active" or row.username != session.username:
            return None
        # Database role is authoritative, so role changes revoke stale privilege.
        session = AdminSession(
            username=row.username,
            expires_at=session.expires_at,
            csrf_token=session.csrf_token,
            user_id=str(row.id),
            role=row.role,
        )
        return session

    async def _authorized(
        self, request: Request, *, csrf: bool = False
    ) -> AdminSession | None:
        session = await asyncio.to_thread(self._session_sync, request)
        if session is None:
            return None
        if csrf and not hmac.compare_digest(
            request.headers.get("x-csrf-token", ""), session.csrf_token
        ):
            return None
        return session

    async def health(self, _request: Request) -> Response:
        try:
            return JSONResponse(await asyncio.to_thread(self.repository.health))
        except Exception:
            return _json_error("database_unavailable", 503)

    async def login(self, request: Request) -> Response:
        try:
            value = await request.json()
        except Exception:
            return _json_error("invalid_json", 400)
        username = str(value.get("username") or "").strip().casefold()
        password = str(value.get("password") or "")
        row = await asyncio.to_thread(self.admin_users.get_by_username, username)
        if row is None or row.status != "active" or not verify_password(
            password, row.password_hash
        ):
            return _json_error("invalid_credentials", 401)
        token, session = self.signer.issue(
            row.username, user_id=str(row.id), role=row.role
        )
        await asyncio.to_thread(self.admin_users.touch_login, row.id)
        response = JSONResponse(
            {
                "authenticated": True,
                "username": username,
                "role": row.role,
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
        session = await self._authorized(request)
        if session is None:
            return _json_error("unauthorized", 401)
        return JSONResponse(
            {
                "authenticated": True,
                "username": session.username,
                "role": session.role,
                "csrf_token": session.csrf_token,
                "expires_at": session.expires_at,
                **self._business_context(),
            }
        )

    async def logout(self, request: Request) -> Response:
        if await self._authorized(request, csrf=True) is None:
            return _json_error("unauthorized_or_csrf", 401)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE_NAME, path="/admin")
        return response

    async def dashboard(self, request: Request) -> Response:
        if await self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        return JSONResponse(await asyncio.to_thread(self.repository.dashboard))

    async def participants(self, request: Request) -> Response:
        if await self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        query = request.query_params
        page, error = self._query_int(request, "page", 1, maximum=1_000_000)
        if error:
            return error
        limit, error = self._query_int(request, "limit", 25, maximum=100)
        if error:
            return error
        return JSONResponse(
            await asyncio.to_thread(
                self.repository.participants,
                search=query.get("search", ""),
                status=query.get("status", ""),
                page=page,
                limit=limit,
            )
        )

    async def _participant(
        self, request: Request
    ) -> tuple[Any, Response | None]:
        if await self._authorized(request) is None:
            return None, _json_error("unauthorized", 401)
        code = request.path_params["participant_code"]
        participant_id = await asyncio.to_thread(
            self.repository.participant_id, code
        )
        if participant_id is None:
            return None, _json_error("participant_not_found", 404)
        return participant_id, None

    async def participant(self, request: Request) -> Response:
        if await self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        value = await asyncio.to_thread(
            self.repository.participant, request.path_params["participant_code"]
        )
        return JSONResponse(value) if value else _json_error("participant_not_found", 404)

    async def messages(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        query = request.query_params
        limit, query_error = self._query_int(
            request, "limit", 50, maximum=200
        )
        if query_error:
            return query_error
        items = await asyncio.to_thread(
            self.repository.messages,
            participant_id,
            status=query.get("status", ""),
            error_only=query.get("error_only", "").lower() in {"1", "true", "yes"},
            limit=limit,
        )
        return JSONResponse({"items": items})

    async def message(self, request: Request) -> Response:
        if await self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        value = await asyncio.to_thread(
            self.repository.message, request.path_params["event_id"]
        )
        return JSONResponse(value) if value else _json_error("message_not_found", 404)

    async def observations(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        return JSONResponse({
            "items": await asyncio.to_thread(
                self.repository.observations, participant_id
            )
        })

    async def calendars(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        return JSONResponse({
            "items": await asyncio.to_thread(
                self.repository.calendars, participant_id
            )
        })

    async def forecasts(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        return JSONResponse({
            "items": await asyncio.to_thread(
                self.repository.forecasts, participant_id
            )
        })

    async def forecast(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        try:
            target = date.fromisoformat(request.path_params["local_date"])
        except ValueError:
            return _json_error("invalid_local_date", 400)
        value = await asyncio.to_thread(
            self.repository.forecast, participant_id, target
        )
        return JSONResponse(value) if value else _json_error("forecast_not_found", 404)

    async def refresh_forecast(self, request: Request) -> Response:
        session = await self._authorized(request, csrf=True)
        if session is None:
            return _json_error("unauthorized_or_csrf", 401)
        if session.role not in {"admin", "superadmin"}:
            return _json_error("forbidden", 403)
        participant_id = await asyncio.to_thread(
            self.repository.participant_id,
            request.path_params["participant_code"],
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
        participant_id, error = await self._participant(request)
        if error:
            return error
        try:
            target = date.fromisoformat(request.path_params["local_date"])
        except ValueError:
            return _json_error("invalid_local_date", 400)
        if self.pressure_curves is None:
            forecast = await asyncio.to_thread(
                self.repository.forecast, participant_id, target
            )
            return JSONResponse(forecast) if forecast else _json_error("forecast_not_found", 404)
        try:
            view = await self.pressure_curves.read_persisted(
                participant_id,
                target,
                stress_only=False,
                render_png=False,
            )
        except HistoricalForecastNotFoundError:
            return _json_error("forecast_not_found", 404)
        output = dict(view.forecast.get("output") or {})
        retrospective = await asyncio.to_thread(
            self.retrospectives.latest, participant_id, target
        )
        current_forecast_version = view.forecast.get("forecast_version")
        retrospective_source = None
        retrospective_matches_current = None
        if retrospective is not None:
            retrospective_matches_current = (
                retrospective.get("source_forecast_version")
                == current_forecast_version
            )
            retrospective_source = await asyncio.to_thread(
                self.forecasts_repository.get,
                participant_id,
                retrospective["source_forecast_id"],
                local_date=target,
            )
            if (
                retrospective_source is not None
                and retrospective_source.get("forecast_version")
                != retrospective.get("source_forecast_version")
            ):
                retrospective_source = None
        reviews, observations = await asyncio.gather(
            asyncio.to_thread(self.review_responses.list, participant_id, target),
            asyncio.to_thread(
                self.observations_repository.for_local_date,
                participant_id,
                target,
                timezone_name=self.settings.timezone_name,
                limit=500,
            ),
        )
        return JSONResponse(
            {
                "local_date": str(view.forecast.get("local_date") or target),
                "forecast_version": current_forecast_version,
                "current_forecast_version": current_forecast_version,
                "forecast_id": str(view.forecast.get("id") or ""),
                "generated_at": view.forecast.get("generated_at"),
                "calendar_revision": view.forecast.get("calendar_revision"),
                "observation_revision": view.forecast.get("observation_revision"),
                "point_count": view.analysis.point_count,
                "is_current": True,
                "curve": list(view.forecast.get("curve") or []),
                "analysis": view.analysis.to_dict(),
                "events": list(view.forecast.get("calendar_events") or []),
                "warnings": list(view.analysis.warning_windows),
                "initial_state": dict(output.get("initial_state") or {}),
                "initial_state_revision": output.get("initial_state_revision"),
                "calendar_degraded": bool(view.forecast.get("calendar_degraded")),
                "semantic_status": view.forecast.get("semantic_status"),
                "retrospective_curve": (
                    list(retrospective.get("curve") or []) if retrospective else []
                ),
                "retrospective": retrospective,
                "retrospective_source_forecast_id": (
                    retrospective.get("source_forecast_id")
                    if retrospective else None
                ),
                "retrospective_source_forecast_version": (
                    retrospective.get("source_forecast_version")
                    if retrospective else None
                ),
                "retrospective_source_curve": (
                    list(retrospective_source.get("curve") or [])
                    if retrospective_source else []
                ),
                "retrospective_matches_current_forecast": (
                    retrospective_matches_current
                ),
                "daily_review_responses": reviews,
                "instant_observations": observations,
                "overlay_labels": {
                    "forecast": "预测", "instant": "即时反馈",
                    "daily_review": "回顾反馈", "posterior": "回顾估计",
                },
            }
        )

    async def daily_reviews_list(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        return JSONResponse({
            "items": await asyncio.to_thread(
                self.review_responses.list, participant_id
            )
        })

    async def daily_reviews_date(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        try:
            target = date.fromisoformat(request.path_params["local_date"])
        except ValueError:
            return _json_error("invalid_local_date", 400)
        items = await asyncio.to_thread(
            self.review_responses.list, participant_id, target
        )
        return JSONResponse({"items": items, "latest": items[0] if items else None})

    async def retrospective_curve(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        try:
            target = date.fromisoformat(request.path_params["local_date"])
        except ValueError:
            return _json_error("invalid_local_date", 400)
        value = await asyncio.to_thread(
            self.retrospectives.latest, participant_id, target
        )
        return JSONResponse(value) if value else _json_error("retrospective_curve_not_found", 404)

    async def rebuild_retrospective_curve(self, request: Request) -> Response:
        session = await self._authorized(request, csrf=True)
        if session is None:
            return _json_error("unauthorized_or_csrf", 401)
        if session.role not in {"admin", "superadmin"}:
            return _json_error("forbidden", 403)
        participant_id = await asyncio.to_thread(
            self.repository.participant_id,
            request.path_params["participant_code"],
        )
        if participant_id is None:
            return _json_error("participant_not_found", 404)
        if self.daily_reviews is None:
            return _json_error("daily_review_service_unavailable", 503)
        try:
            target = date.fromisoformat(request.path_params["local_date"])
            value = await asyncio.to_thread(
                self.daily_reviews.rebuild, participant_id, target
            )
        except ValueError as exc:
            return _json_error(str(exc), 409)
        return JSONResponse(value)

    async def reanalyse_retrospective_curve(self, request: Request) -> Response:
        session = await self._authorized(request, csrf=True)
        if session is None:
            return _json_error("unauthorized_or_csrf", 401)
        if session.role not in {"admin", "superadmin"}:
            return _json_error("forbidden", 403)
        participant_id = await asyncio.to_thread(
            self.repository.participant_id,
            request.path_params["participant_code"],
        )
        if participant_id is None:
            return _json_error("participant_not_found", 404)
        if self.daily_reviews is None:
            return _json_error("daily_review_service_unavailable", 503)
        try:
            target = date.fromisoformat(request.path_params["local_date"])
            value = await asyncio.to_thread(
                self.daily_reviews.reanalysis, participant_id, target
            )
        except ValueError as exc:
            return _json_error(str(exc), 409)
        return JSONResponse(value)

    async def curve_png(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        if self.pressure_curves is None:
            return _json_error("forecast_service_unavailable", 503)
        try:
            target = date.fromisoformat(request.path_params["local_date"])
        except ValueError:
            return _json_error("invalid_local_date", 400)
        try:
            view = await self.pressure_curves.read_persisted(
                participant_id,
                target,
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
        participant_id, error = await self._participant(request)
        if error:
            return error
        return JSONResponse({
            "items": await asyncio.to_thread(
                self.repository.warnings, participant_id
            )
        })

    async def care_timeline(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        limit, query_error = self._query_int(
            request, "limit", 100, maximum=500
        )
        if query_error:
            return query_error
        return JSONResponse(
            await asyncio.to_thread(
                self.repository.care_timeline, participant_id, limit=limit
            )
        )

    async def research_dashboard(self, request: Request) -> Response:
        if await self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        date_start, date_end, error = self._research_dates(request)
        if error:
            return error
        return JSONResponse(
            await asyncio.to_thread(
                self.research.cohort_dashboard, date_start, date_end
            )
        )

    async def data_quality(self, request: Request) -> Response:
        if await self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        date_start, date_end, error = self._research_dates(request)
        if error:
            return error
        participant_id = None
        participant_code = str(
            request.query_params.get("participant_code") or ""
        ).strip()
        if participant_code:
            participant_id = await asyncio.to_thread(
                self.repository.participant_id, participant_code
            )
            if participant_id is None:
                return _json_error("participant_not_found", 404)
        return JSONResponse(
            await asyncio.to_thread(
                self.research.data_quality,
                date_start,
                date_end,
                participant_id,
            )
        )

    async def participant_longitudinal(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        try:
            through = date.fromisoformat(
                request.query_params.get("through")
                or datetime.now(ZoneInfo(self.settings.timezone_name))
                .date()
                .isoformat()
            )
            days = int(request.query_params.get("days") or 14)
            if days not in {7, 14}:
                raise ValueError
        except (TypeError, ValueError):
            return _json_error("invalid_query_parameter", 400)
        return JSONResponse(
            await asyncio.to_thread(
                self.research.participant_longitudinal,
                participant_id,
                through,
                days,
            )
        )

    async def participant_evaluation(self, request: Request) -> Response:
        participant_id, error = await self._participant(request)
        if error:
            return error
        date_start, date_end, error = self._research_dates(request)
        if error:
            return error
        return JSONResponse(
            await asyncio.to_thread(
                self.research.evaluation,
                date_start,
                date_end,
                participant_id,
            )
        )

    async def rebuild_research_matches(self, request: Request) -> Response:
        session = await self._authorized(request, csrf=True)
        if session is None:
            return _json_error("unauthorized_or_csrf", 401)
        if session.role not in {"admin", "superadmin"}:
            return _json_error("forbidden", 403)
        date_start, date_end, error = self._research_dates(request)
        if error:
            return error
        participant_id = None
        participant_code = str(
            request.query_params.get("participant_code") or ""
        ).strip()
        if participant_code:
            participant_id = await asyncio.to_thread(
                self.repository.participant_id, participant_code
            )
            if participant_id is None:
                return _json_error("participant_not_found", 404)
        result = await asyncio.to_thread(
            self.research.rebuild_matches,
            date_start=date_start,
            date_end=date_end,
            participant_id=participant_id,
        )
        return JSONResponse(result)

    async def dataset_snapshots(self, request: Request) -> Response:
        session = await self._authorized(request, csrf=request.method == "POST")
        if session is None:
            return _json_error(
                "unauthorized_or_csrf" if request.method == "POST" else "unauthorized",
                401,
            )
        if request.method == "GET":
            limit, error = self._query_int(
                request, "limit", 50, maximum=200
            )
            if error:
                return error
            return JSONResponse(
                {"items": await asyncio.to_thread(self.research.list_snapshots, limit)}
            )
        if session.role not in {"admin", "superadmin"}:
            return _json_error("forbidden", 403)
        try:
            value = await request.json()
            today = datetime.now(ZoneInfo(self.settings.timezone_name)).date()
            date_end = date.fromisoformat(
                str(value.get("date_end") or today.isoformat())
            )
            date_start = date.fromisoformat(
                str(
                    value.get("date_start")
                    or (date_end - timedelta(days=13)).isoformat()
                )
            )
            participant_filter = value.get("participant_filter") or {}
            if not isinstance(participant_filter, dict):
                raise ValueError("participant_filter must be an object")
            cutoffs = {}
            for field in ("observation_cutoff", "calendar_cutoff"):
                raw = value.get(field)
                if raw:
                    parsed = datetime.fromisoformat(
                        str(raw).replace("Z", "+00:00")
                    )
                    if parsed.tzinfo is None:
                        raise ValueError(f"{field} must include timezone")
                    cutoffs[field] = parsed.astimezone(timezone.utc)
            item = await asyncio.to_thread(
                self.research.create_dataset_snapshot,
                date_start=date_start,
                date_end=date_end,
                participant_filter=participant_filter,
                **cutoffs,
            )
        except (TypeError, ValueError) as exc:
            return _json_error(str(exc) or "invalid_request", 400)
        return JSONResponse(item, status_code=201)

    async def dataset_snapshot_items(self, request: Request) -> Response:
        if await self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        try:
            snapshot_id = uuid.UUID(request.path_params["snapshot_id"])
            item_type = str(request.query_params.get("item_type") or "").strip()
            if item_type and item_type not in {
                "observation", "forecast", "forecast_currentness",
                "calendar", "match_source",
            }:
                raise ValueError("unsupported item_type")
            items = await asyncio.to_thread(
                self.research.snapshot_items,
                snapshot_id,
                item_type or None,
            )
        except (TypeError, ValueError) as exc:
            return _json_error(str(exc) or "invalid_request", 400)
        return JSONResponse({"items": items})

    async def evaluation_runs(self, request: Request) -> Response:
        session = await self._authorized(request, csrf=request.method == "POST")
        if session is None:
            return _json_error(
                "unauthorized_or_csrf" if request.method == "POST" else "unauthorized",
                401,
            )
        if request.method == "GET":
            limit, error = self._query_int(
                request, "limit", 100, maximum=500
            )
            if error:
                return error
            return JSONResponse(
                {"items": await asyncio.to_thread(self.research.list_runs, limit)}
            )
        if session.role not in {"admin", "superadmin"}:
            return _json_error("forbidden", 403)
        try:
            value = await request.json()
            snapshot_id = uuid.UUID(str(value.get("dataset_snapshot_id") or ""))
            model_version = str(value.get("model_version") or "").strip()
            evaluation_mode = str(
                value.get("evaluation_mode") or "historical_online"
            ).strip()
            if not model_version:
                raise ValueError("model_version is required")
            participant_id = None
            participant_code = str(value.get("participant_code") or "").strip()
            if participant_code:
                participant_id = await asyncio.to_thread(
                    self.repository.participant_id, participant_code
                )
                if participant_id is None:
                    return _json_error("participant_not_found", 404)
            item = await asyncio.to_thread(
                self.research.create_evaluation_run,
                snapshot_id,
                model_version,
                participant_id,
                evaluation_mode,
            )
        except (TypeError, ValueError) as exc:
            return _json_error(str(exc) or "invalid_request", 400)
        return JSONResponse(item, status_code=201)

    async def incidents(self, request: Request) -> Response:
        if await self._authorized(request) is None:
            return _json_error("unauthorized", 401)
        limit, error = self._query_int(request, "limit", 100, maximum=500)
        if error:
            return error
        return JSONResponse(
            {"items": await asyncio.to_thread(self.repository.incidents, limit)}
        )

    async def admin_users_list(self, request: Request) -> Response:
        session = await self._authorized(request)
        if session is None:
            return _json_error("unauthorized", 401)
        if session.role != "superadmin":
            return _json_error("forbidden", 403)
        return JSONResponse({
            "items": await asyncio.to_thread(self.admin_users.list),
            "roles": ["viewer", "admin", "superadmin"],
        })

    async def admin_users_create(self, request: Request) -> Response:
        session = await self._authorized(request, csrf=True)
        if session is None:
            return _json_error("unauthorized_or_csrf", 401)
        if session.role != "superadmin":
            return _json_error("forbidden", 403)
        try:
            value = await request.json()
            password = str(value.get("password") or "")
            if len(password) < 10:
                raise ValueError("password must be at least 10 characters")
            item = await asyncio.to_thread(
                self.admin_users.create,
                str(value.get("username") or ""),
                hash_password(password),
                str(value.get("role") or "viewer"),
                created_by=uuid.UUID(session.user_id),
            )
        except (ValueError, TypeError) as exc:
            return _json_error(str(exc), 400)
        return JSONResponse(item, status_code=201)

    async def admin_users_update(self, request: Request) -> Response:
        session = await self._authorized(request, csrf=True)
        if session is None:
            return _json_error("unauthorized_or_csrf", 401)
        if session.role != "superadmin":
            return _json_error("forbidden", 403)
        try:
            value = await request.json()
            password = value.get("password")
            if password is not None and len(str(password)) < 10:
                raise ValueError("password must be at least 10 characters")
            item = await asyncio.to_thread(
                self.admin_users.update,
                request.path_params["admin_id"],
                role=value.get("role"),
                status=value.get("status"),
                password_hash=hash_password(str(password)) if password else None,
                actor_id=uuid.UUID(session.user_id),
            )
        except (ValueError, TypeError) as exc:
            return _json_error(str(exc), 400)
        return JSONResponse(item) if item else _json_error("admin_not_found", 404)

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
            Route(f"{prefix}/participants/{{participant_code}}/care-timeline", self.care_timeline, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/daily-reviews", self.daily_reviews_list, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/daily-reviews/{{local_date}}", self.daily_reviews_date, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/retrospective-curve/{{local_date}}", self.retrospective_curve, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/retrospective-curve/{{local_date}}/rebuild", self.rebuild_retrospective_curve, methods=["POST"]),
            Route(f"{prefix}/participants/{{participant_code}}/retrospective-curve/{{local_date}}/reanalysis", self.reanalyse_retrospective_curve, methods=["POST"]),
            Route(f"{prefix}/research/dashboard", self.research_dashboard, methods=["GET"]),
            Route(f"{prefix}/data-quality", self.data_quality, methods=["GET"]),
            Route(f"{prefix}/research/matches/rebuild", self.rebuild_research_matches, methods=["POST"]),
            Route(f"{prefix}/research/dataset-snapshots", self.dataset_snapshots, methods=["GET", "POST"]),
            Route(f"{prefix}/research/dataset-snapshots/{{snapshot_id}}/items", self.dataset_snapshot_items, methods=["GET"]),
            Route(f"{prefix}/research/evaluation-runs", self.evaluation_runs, methods=["GET", "POST"]),
            Route(f"{prefix}/participants/{{participant_code}}/longitudinal", self.participant_longitudinal, methods=["GET"]),
            Route(f"{prefix}/participants/{{participant_code}}/evaluation", self.participant_evaluation, methods=["GET"]),
            Route(f"{prefix}/incidents", self.incidents, methods=["GET"]),
            Route(f"{prefix}/admin-users", self.admin_users_list, methods=["GET"]),
            Route(f"{prefix}/admin-users", self.admin_users_create, methods=["POST"]),
            Route(f"{prefix}/admin-users/{{admin_id}}", self.admin_users_update, methods=["PATCH"]),
        ]
