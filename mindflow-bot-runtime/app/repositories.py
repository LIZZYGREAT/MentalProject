"""Participant-scoped repositories. Every user query takes a trusted UUID."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import uuid
from typing import Any, Optional

from sqlalchemy import desc, or_, select
from sqlalchemy.exc import IntegrityError

from app.db import Database
from app.models import (
    AgentRun,
    AgentToolCall,
    BotEvent,
    CalendarSnapshot,
    ClaudeSession,
    ConversationMessage,
    EventSemanticCache,
    FeishuBinding,
    FeishuOAuthToken,
    Participant,
    ParticipantProfile,
    LearnedModelProfile,
    ForecastSnapshot,
    PredictionRun,
    RuntimeIncident,
    StateObservation,
    WarningSchedule,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@dataclass(frozen=True)
class ParticipantView:
    id: uuid.UUID
    participant_code: str
    status: str
    external_llm_consent_at: datetime | None = None


@dataclass(frozen=True)
class RecoverableBotEvent:
    event_id: str
    message_id: str
    app_id: str
    open_id: str
    chat_id: str
    chat_type: str
    text: str
    create_time: datetime


@dataclass(frozen=True)
class PendingReplyPlan:
    event_id: str
    full_text: str
    segments: tuple[str, ...]
    next_segment: int
    message_ids: tuple[str, ...]
    plan_version: str


@dataclass(frozen=True)
class ClaudeSessionView:
    participant_id: uuid.UUID
    session_id: str
    status: str
    last_message_id: str | None
    created_at: datetime
    updated_at: datetime


class ParticipantRepository:
    def __init__(self, database: Database):
        self.database = database

    def create(self, participant_code: str) -> ParticipantView:
        with self.database.session() as session:
            row = Participant(participant_code=str(participant_code).strip())
            session.add(row)
            session.flush()
            return ParticipantView(
                row.id, row.participant_code, row.status, row.external_llm_consent_at
            )

    def get(self, participant_id: uuid.UUID) -> Optional[ParticipantView]:
        with self.database.session() as session:
            row = session.get(Participant, participant_id)
            if row is None:
                return None
            return ParticipantView(
                row.id, row.participant_code, row.status, row.external_llm_consent_at
            )

    def get_by_code(self, participant_code: str) -> Optional[ParticipantView]:
        with self.database.session() as session:
            row = session.execute(
                select(Participant).where(
                    Participant.participant_code == str(participant_code).strip()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return ParticipantView(
                row.id, row.participant_code, row.status, row.external_llm_consent_at
            )

    def set_external_llm_consent(
        self, participant_id: uuid.UUID, *, allowed: bool
    ) -> ParticipantView:
        with self.database.session() as session:
            row = session.get(Participant, participant_id, with_for_update=True)
            if row is None:
                raise ValueError("participant not found")
            row.external_llm_consent_at = utc_now() if allowed else None
            return ParticipantView(
                row.id, row.participant_code, row.status, row.external_llm_consent_at
            )

    def active_ids(self) -> list[uuid.UUID]:
        with self.database.session() as session:
            return list(session.execute(
                select(Participant.id).where(Participant.status == "active")
            ).scalars().all())

    def active_calendar_ids(self, oauth_app_id: str) -> list[uuid.UUID]:
        """Return active participants authorized by the current Calendar app."""

        with self.database.session() as session:
            return list(session.execute(
                select(Participant.id)
                .join(
                    FeishuOAuthToken,
                    FeishuOAuthToken.participant_id == Participant.id,
                )
                .where(
                    Participant.status == "active",
                    FeishuOAuthToken.oauth_app_id == oauth_app_id,
                )
            ).scalars().all())


class BindingRepository:
    def __init__(self, database: Database):
        self.database = database

    def get_by_app_and_open_id(
        self, app_id: str, open_id: str
    ) -> Optional[ParticipantView]:
        with self.database.session() as session:
            row = session.execute(
                select(Participant)
                .join(FeishuBinding, FeishuBinding.participant_id == Participant.id)
                .where(
                    FeishuBinding.app_id == str(app_id),
                    FeishuBinding.open_id == str(open_id),
                    Participant.status == "active",
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return ParticipantView(
                row.id, row.participant_code, row.status, row.external_llm_consent_at
            )

    def get_for_participant(self, participant_id: uuid.UUID) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(
                select(FeishuBinding).where(
                    FeishuBinding.participant_id == participant_id
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {"app_id": row.app_id, "open_id": row.open_id, "chat_id": row.chat_id}


class ProfileRepository:
    def __init__(self, database: Database):
        self.database = database

    def current(self, participant_id: uuid.UUID) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(
                select(ParticipantProfile)
                .where(ParticipantProfile.participant_id == participant_id)
                .order_by(desc(ParticipantProfile.version))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "version": row.version,
                "profile": dict(row.profile_json),
                "created_at": row.created_at.isoformat(),
            }

    def save(self, participant_id: uuid.UUID, profile: dict[str, Any]) -> int:
        with self.database.session() as session:
            latest = session.execute(
                select(ParticipantProfile.version)
                .where(ParticipantProfile.participant_id == participant_id)
                .order_by(desc(ParticipantProfile.version))
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()
            version = int(latest or 0) + 1
            session.add(
                ParticipantProfile(
                    participant_id=participant_id,
                    version=version,
                    profile_json=dict(profile),
                )
            )
            return version


class LearnedProfileRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row: LearnedModelProfile) -> dict[str, Any]:
        return {
            "version": row.version,
            "parameters": dict(row.parameters_json),
            "source": row.source,
            "sample_count": row.sample_count,
            "day_count": row.day_count,
            "confidence": row.confidence,
            "window_start": row.window_start.isoformat(),
            "window_end": row.window_end.isoformat(),
            "created_at": row.created_at.isoformat(),
        }

    def current(self, participant_id: uuid.UUID) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(select(LearnedModelProfile).where(
                LearnedModelProfile.participant_id == participant_id
            ).order_by(desc(LearnedModelProfile.version)).limit(1)).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def save(
        self, participant_id: uuid.UUID, *, parameters: dict[str, Any],
        sample_count: int, day_count: int, confidence: float,
        window_start: date, window_end: date, source: str = "calibration.v1",
    ) -> dict[str, Any]:
        with self.database.session() as session:
            session.get(Participant, participant_id, with_for_update=True)
            latest = session.execute(select(LearnedModelProfile.version).where(
                LearnedModelProfile.participant_id == participant_id
            ).order_by(desc(LearnedModelProfile.version)).limit(1)).scalar_one_or_none()
            row = LearnedModelProfile(
                participant_id=participant_id, version=int(latest or 0) + 1,
                parameters_json=dict(parameters), source=source,
                sample_count=sample_count, day_count=day_count,
                confidence=max(0.0, min(1.0, float(confidence))),
                window_start=window_start, window_end=window_end,
            )
            session.add(row)
            session.flush()
            return self._view(row)


class ObservationRepository:
    def __init__(self, database: Database):
        self.database = database

    def add(
        self,
        participant_id: uuid.UUID,
        observation_type: str,
        payload: dict[str, Any],
        *,
        observed_at: Optional[datetime] = None,
        source_message_id: Optional[str] = None,
    ) -> uuid.UUID:
        def find_existing(session: Any) -> StateObservation | None:
            if not source_message_id:
                return None
            return session.execute(
                select(StateObservation).where(
                    StateObservation.participant_id == participant_id,
                    StateObservation.source_message_id == source_message_id,
                    StateObservation.observation_type == observation_type,
                )
            ).scalar_one_or_none()

        try:
            with self.database.session() as session:
                existing = find_existing(session)
                if existing is not None:
                    return existing.id
                row = StateObservation(
                    participant_id=participant_id,
                    observation_type=observation_type,
                    source_message_id=source_message_id,
                    payload_json=dict(payload),
                    observed_at=observed_at or utc_now(),
                )
                session.add(row)
                session.flush()
                return row.id
        except IntegrityError:
            # A competing request can commit after the SELECT but before this
            # INSERT. The database constraint is authoritative; return the row
            # that won the race instead of turning an idempotent retry into 500.
            with self.database.session() as session:
                existing = find_existing(session)
                if existing is not None:
                    return existing.id
            raise

    def recent(self, participant_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(StateObservation)
                .where(StateObservation.participant_id == participant_id)
                .order_by(
                    desc(StateObservation.observed_at),
                    desc(StateObservation.created_at),
                    desc(StateObservation.id),
                )
                .limit(max(1, min(int(limit), 100)))
            ).scalars()
            return [
                {
                    "id": str(row.id),
                    "type": row.observation_type,
                    "payload": dict(row.payload_json),
                    "observed_at": row.observed_at.isoformat(),
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]


class PredictionRepository:
    def __init__(self, database: Database):
        self.database = database

    def save(
        self,
        participant_id: uuid.UUID,
        *,
        profile_version: Optional[int],
        model_version: str,
        input_snapshot: dict[str, Any],
        output: dict[str, Any],
        source_message_id: Optional[str] = None,
    ) -> uuid.UUID:
        with self.database.session() as session:
            if source_message_id:
                existing = session.execute(
                    select(PredictionRun).where(
                        PredictionRun.participant_id == participant_id,
                        PredictionRun.source_message_id == source_message_id,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return existing.id
            row = PredictionRun(
                participant_id=participant_id,
                profile_version=profile_version,
                source_message_id=source_message_id,
                model_version=model_version,
                input_snapshot_json=dict(input_snapshot),
                output_json=dict(output),
            )
            session.add(row)
            session.flush()
            return row.id

    def by_source_message(
        self, participant_id: uuid.UUID, source_message_id: str
    ) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(
                select(PredictionRun).where(
                    PredictionRun.participant_id == participant_id,
                    PredictionRun.source_message_id == source_message_id,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {"prediction_run_id": str(row.id), **dict(row.output_json)}

    def latest(self, participant_id: uuid.UUID) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(
                select(PredictionRun)
                .where(PredictionRun.participant_id == participant_id)
                .order_by(desc(PredictionRun.created_at))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": str(row.id),
                "profile_version": row.profile_version,
                "model_version": row.model_version,
                "output": dict(row.output_json),
                "created_at": row.created_at.isoformat(),
            }


class CalendarSnapshotRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row: CalendarSnapshot) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(),
            "calendar_revision": row.calendar_revision,
            "events": list(row.events_json),
            "degraded": row.degraded,
            "last_refresh_attempt_at": (
                row.last_refresh_attempt_at.isoformat() if row.last_refresh_attempt_at else None
            ),
            "last_refresh_success_at": (
                row.last_refresh_success_at.isoformat() if row.last_refresh_success_at else None
            ),
            "last_refresh_error_class": row.last_refresh_error_class,
            "updated_at": row.updated_at.isoformat(),
        }

    def get(self, participant_id: uuid.UUID, local_date: date) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(
                select(CalendarSnapshot).where(
                    CalendarSnapshot.participant_id == participant_id,
                    CalendarSnapshot.local_date == local_date,
                )
            ).scalar_one_or_none()
            return self._view(row) if row else None

    def upsert(
        self, participant_id: uuid.UUID, local_date: date, *, revision: str,
        events: list[dict[str, Any]], degraded: bool,
        refresh_error_class: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self.database.session() as session:
            row = session.execute(
                select(CalendarSnapshot).where(
                    CalendarSnapshot.participant_id == participant_id,
                    CalendarSnapshot.local_date == local_date,
                ).with_for_update()
            ).scalar_one_or_none()
            changed = row is None or row.calendar_revision != revision
            now = utc_now()
            if row is None:
                row = CalendarSnapshot(
                    participant_id=participant_id, local_date=local_date,
                    calendar_revision=revision, events_json=list(events), degraded=degraded,
                    last_refresh_attempt_at=now,
                    last_refresh_success_at=None if degraded else now,
                    last_refresh_error_class=refresh_error_class,
                )
                session.add(row)
            elif changed or row.degraded != degraded:
                row.calendar_revision = revision
                row.events_json = list(events)
                row.degraded = degraded
                row.updated_at = now
            row.last_refresh_attempt_at = now
            if not degraded:
                row.last_refresh_success_at = now
                row.last_refresh_error_class = None
            elif refresh_error_class:
                row.last_refresh_error_class = refresh_error_class
            session.flush()
            return self._view(row), changed


class EventSemanticCacheRepository:
    def __init__(self, database: Database):
        self.database = database

    def get(
        self, participant_id: uuid.UUID, fingerprint: str, *,
        schema_version: str, prompt_version: str, model: str,
    ) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(
                select(EventSemanticCache).where(
                    EventSemanticCache.participant_id == participant_id,
                    EventSemanticCache.fingerprint == fingerprint,
                    EventSemanticCache.schema_version == schema_version,
                    EventSemanticCache.prompt_version == prompt_version,
                    EventSemanticCache.model == model,
                    EventSemanticCache.status == "complete",
                )
            ).scalar_one_or_none()
            return dict(row.assessment_json) if row else None

    def put(
        self, participant_id: uuid.UUID, fingerprint: str, assessment: dict[str, Any], *,
        schema_version: str, prompt_version: str, model: str,
    ) -> None:
        with self.database.session() as session:
            row = session.execute(
                select(EventSemanticCache).where(
                    EventSemanticCache.participant_id == participant_id,
                    EventSemanticCache.fingerprint == fingerprint,
                    EventSemanticCache.schema_version == schema_version,
                    EventSemanticCache.prompt_version == prompt_version,
                    EventSemanticCache.model == model,
                ).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                session.add(EventSemanticCache(
                    participant_id=participant_id, fingerprint=fingerprint,
                    schema_version=schema_version, prompt_version=prompt_version,
                    model=model, assessment_json=dict(assessment), status="complete",
                ))
            else:
                row.assessment_json = dict(assessment)
                row.status = "complete"
                row.updated_at = utc_now()


class ForecastSnapshotRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row: ForecastSnapshot) -> dict[str, Any]:
        return {
            "id": str(row.id), "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(),
            "calendar_revision": row.calendar_revision,
            "semantic_revision": row.semantic_revision,
            "observation_revision": row.observation_revision,
            "algorithm_version": row.algorithm_version,
            "forecast_version": row.forecast_version,
            "semantic_status": row.semantic_status,
            "semantic_input": list(row.semantic_input_json),
            "curve": list(row.curve_json), "peaks": list(row.peaks_json),
            "warning_windows": list(row.warning_windows_json),
            "output": dict(row.output_json), "valid": row.valid,
            "generated_at": row.generated_at.isoformat(),
        }

    def latest(self, participant_id: uuid.UUID, local_date: date) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(
                select(ForecastSnapshot).where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date == local_date,
                    ForecastSnapshot.valid.is_(True),
                ).order_by(desc(ForecastSnapshot.generated_at)).limit(1)
            ).scalar_one_or_none()
            return self._view(row) if row else None

    def history(
        self, participant_id: uuid.UUID, *, through: date, days: int = 14
    ) -> list[dict[str, Any]]:
        start = through - timedelta(days=max(1, days) - 1)
        with self.database.session() as session:
            rows = session.execute(select(ForecastSnapshot).where(
                ForecastSnapshot.participant_id == participant_id,
                ForecastSnapshot.local_date >= start,
                ForecastSnapshot.local_date <= through,
                ForecastSnapshot.valid.is_(True),
            ).order_by(ForecastSnapshot.local_date, desc(ForecastSnapshot.generated_at))).scalars().all()
            # Only the latest valid snapshot for each date participates.
            result: list[dict[str, Any]] = []
            seen: set[date] = set()
            for row in rows:
                if row.local_date in seen:
                    continue
                seen.add(row.local_date)
                result.append(self._view(row))
            return result

    def latest_before(
        self, participant_id: uuid.UUID, local_date: date, timestamp: datetime
    ) -> Optional[dict[str, Any]]:
        """Return the newest snapshot generated before a causal cutoff.

        Invalidated snapshots are intentionally included: a later forecast may
        invalidate the exact pre-observation version that calibration needs.
        """

        with self.database.session() as session:
            row = session.execute(select(ForecastSnapshot).where(
                ForecastSnapshot.participant_id == participant_id,
                ForecastSnapshot.local_date == local_date,
                ForecastSnapshot.generated_at < timestamp,
            ).order_by(desc(ForecastSnapshot.generated_at)).limit(1)).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def save(
        self, participant_id: uuid.UUID, local_date: date, *,
        calendar_revision: str, semantic_revision: str, algorithm_version: str,
        forecast_version: str, semantic_status: str, semantic_input: list[dict[str, Any]],
        curve: list[dict[str, Any]], peaks: list[dict[str, Any]],
        warning_windows: list[dict[str, Any]], output: dict[str, Any],
        observation_revision: str = "",
    ) -> dict[str, Any]:
        with self.database.session() as session:
            return self._save_in_session(
                session, participant_id, local_date,
                calendar_revision=calendar_revision, semantic_revision=semantic_revision,
                observation_revision=observation_revision,
                algorithm_version=algorithm_version, forecast_version=forecast_version,
                semantic_status=semantic_status, semantic_input=semantic_input,
                curve=curve, peaks=peaks, warning_windows=warning_windows, output=output,
            )

    def save_and_sync_warnings(
        self, warning_repository: "WarningScheduleRepository",
        participant_id: uuid.UUID, local_date: date, *,
        calendar_revision: str, semantic_revision: str, algorithm_version: str,
        forecast_version: str, semantic_status: str, semantic_input: list[dict[str, Any]],
        curve: list[dict[str, Any]], peaks: list[dict[str, Any]],
        warning_windows: list[dict[str, Any]], output: dict[str, Any],
        warnings: list[dict[str, Any]], now: datetime,
        observation_revision: str = "",
    ) -> tuple[dict[str, Any], dict[str, int]]:
        if warning_repository.database is not self.database:
            raise ValueError("forecast and warning repositories must share a database")
        with self.database.session() as session:
            saved = self._save_in_session(
                session, participant_id, local_date,
                calendar_revision=calendar_revision, semantic_revision=semantic_revision,
                observation_revision=observation_revision,
                algorithm_version=algorithm_version, forecast_version=forecast_version,
                semantic_status=semantic_status, semantic_input=semantic_input,
                curve=curve, peaks=peaks, warning_windows=warning_windows, output=output,
            )
            warning_diff = warning_repository._sync_in_session(
                session, participant_id, local_date,
                forecast_id=uuid.UUID(saved["id"]), forecast_version=forecast_version,
                warnings=warnings, now=now,
            )
            return saved, warning_diff

    def reconcile_warning_derivatives(
        self,
        warning_repository: "WarningScheduleRepository",
        participant_id: uuid.UUID,
        local_date: date,
        *,
        forecast_id: uuid.UUID,
        forecast_version: str,
        selected_candidates: list[dict[str, Any]],
        warning_windows: list[dict[str, Any]],
        warning_revision: str,
        warning_policy_config: dict[str, object],
        warnings: list[dict[str, Any]],
        now: datetime,
    ) -> tuple[dict[str, Any], dict[str, int], bool]:
        """Repair deterministic warning derivatives without rerunning forecast."""

        if warning_repository.database is not self.database:
            raise ValueError("forecast and warning repositories must share a database")
        empty_diff = {
            "kept": 0,
            "created": 0,
            "rescheduled": 0,
            "cancelled": 0,
        }
        with self.database.session() as session:
            row = session.execute(
                select(ForecastSnapshot).where(
                    ForecastSnapshot.id == forecast_id,
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date == local_date,
                    ForecastSnapshot.forecast_version == forecast_version,
                ).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise ValueError("forecast snapshot changed before warning reconciliation")

            current_output = dict(row.output_json)
            derivatives_match = (
                _canonical_json(current_output.get("selected_warning_candidates"))
                == _canonical_json(selected_candidates)
                and _canonical_json(row.warning_windows_json)
                == _canonical_json(warning_windows)
                and current_output.get("warning_revision") == warning_revision
                and _canonical_json(current_output.get("warning_policy_config"))
                == _canonical_json(warning_policy_config)
            )
            if derivatives_match:
                return self._view(row), empty_diff, False

            current_output["selected_warning_candidates"] = list(
                selected_candidates
            )
            current_output["warning_revision"] = warning_revision
            current_output["warning_policy_config"] = dict(
                warning_policy_config
            )
            row.output_json = current_output
            row.warning_windows_json = list(warning_windows)
            warning_diff = warning_repository._sync_in_session(
                session,
                participant_id,
                local_date,
                forecast_id=row.id,
                forecast_version=forecast_version,
                warnings=warnings,
                now=now,
            )
            session.flush()
            return self._view(row), warning_diff, True

    def _save_in_session(
        self, session: Any, participant_id: uuid.UUID, local_date: date, *,
        calendar_revision: str, semantic_revision: str, algorithm_version: str,
        forecast_version: str, semantic_status: str, semantic_input: list[dict[str, Any]],
        curve: list[dict[str, Any]], peaks: list[dict[str, Any]],
        warning_windows: list[dict[str, Any]], output: dict[str, Any],
        observation_revision: str = "",
    ) -> dict[str, Any]:
        existing = session.execute(
            select(ForecastSnapshot).where(
                ForecastSnapshot.participant_id == participant_id,
                ForecastSnapshot.local_date == local_date,
                ForecastSnapshot.forecast_version == forecast_version,
            ).with_for_update()
        ).scalar_one_or_none()
        stale = session.execute(
            select(ForecastSnapshot).where(
                ForecastSnapshot.participant_id == participant_id,
                ForecastSnapshot.local_date == local_date,
                ForecastSnapshot.valid.is_(True),
            ).with_for_update()
        ).scalars().all()
        for row in stale:
            if existing is None or row.id != existing.id:
                row.valid = False
        if existing is not None:
            existing.valid = True
            session.flush()
            return self._view(existing)
        row = ForecastSnapshot(
            participant_id=participant_id, local_date=local_date,
            calendar_revision=calendar_revision, semantic_revision=semantic_revision,
            observation_revision=observation_revision,
            algorithm_version=algorithm_version, forecast_version=forecast_version,
            semantic_status=semantic_status, semantic_input_json=list(semantic_input),
            curve_json=list(curve), peaks_json=list(peaks),
            warning_windows_json=list(warning_windows), output_json=dict(output), valid=True,
        )
        session.add(row)
        session.flush()
        return self._view(row)


class WarningScheduleRepository:
    ACTIVE = {"pending", "claimed", "delivery_unavailable"}
    SUCCESSFUL = {"sent", "escalated"}
    MAX_DAILY_SENDS = 2
    MIN_INTERVAL_MINUTES = 240

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    @staticmethod
    def episode_trigger_fingerprint(
        payload: dict[str, Any] | None, *, default_source: str | None = None,
    ) -> str | None:
        """Return a stable risk-source key without relying on description text."""
        data = payload or {}
        explicit = str(data.get("episode_trigger_fingerprint") or "").strip()
        if explicit and data.get("episode_trigger_fingerprint_version") == 2:
            return explicit

        event_values = []
        for key in (
            "event_identity", "event_id", "source_event_id", "calendar_event_id",
            "dominant_event_id", "current_event_ids",
        ):
            value = data.get(key)
            if isinstance(value, (list, tuple, set)):
                event_values.extend(str(item).strip() for item in value if str(item).strip())
            elif value is not None and str(value).strip():
                event_values.append(str(value).strip())
        source = str(data.get("trigger_source") or default_source or "").strip().casefold()
        if event_values:
            basis = {"kind": "event", "values": sorted(set(event_values))}
            if source:
                basis["trigger_source"] = source
        else:
            stressors = sorted({
                str(value).strip().casefold()
                for value in (data.get("dominant_stressors") or [])
                if str(value).strip()
            })
            if source:
                basis = {"kind": "source", "values": [source]}
            elif stressors:
                # Explanatory model output is intentionally the last fallback:
                # its membership may drift while the real episode stays the same.
                basis = {"kind": "stressor", "values": stressors}
            elif "time" in data:
                # Forecast alerts historically did not always persist a source.
                # Their model-level source is the generic trajectory episode.
                basis = {"kind": "source", "values": ["trajectory_episode"]}
            else:
                return explicit or None
        canonical = json.dumps(
            basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _view(row: WarningSchedule) -> dict[str, Any]:
        return {
            "id": str(row.id), "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(), "forecast_id": str(row.forecast_id),
            "forecast_version": row.forecast_version, "warning_identity": row.warning_identity,
            "episode_identity": row.episode_identity,
            "target_time": row.target_time.isoformat(), "risk_time": row.risk_time.isoformat(),
            "valid_until": row.valid_until.isoformat(), "warning_level": row.warning_level,
            "status": row.status, "payload": dict(row.payload_json),
            "attempt_count": row.attempt_count,
            "claim_token": str(row.claim_token) if row.claim_token else None,
            "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
            "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
            "lease_until": row.lease_until.isoformat() if row.lease_until else None,
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        }

    def sync(
        self, participant_id: uuid.UUID, local_date: date, *, forecast_id: uuid.UUID,
        forecast_version: str, warnings: list[dict[str, Any]], now: datetime,
    ) -> dict[str, int]:
        now = self._aware(now)
        with self.database.session() as session:
            return self._sync_in_session(
                session, participant_id, local_date, forecast_id=forecast_id,
                forecast_version=forecast_version, warnings=warnings, now=now,
            )

    def _sync_in_session(
        self, session: Any, participant_id: uuid.UUID, local_date: date, *,
        forecast_id: uuid.UUID, forecast_version: str,
        warnings: list[dict[str, Any]], now: datetime,
    ) -> dict[str, int]:
        counts = {"kept": 0, "created": 0, "rescheduled": 0, "cancelled": 0}
        now = self._aware(now)
        desired = [
            item for item in warnings
            if self._aware(item["valid_until"]) >= now
            and self._aware(item["risk_time"]) > now
        ]
        rows = session.execute(
            select(WarningSchedule).where(
                WarningSchedule.participant_id == participant_id,
                WarningSchedule.local_date == local_date,
            ).with_for_update()
        ).scalars().all()
        unmatched = {row.id: row for row in rows}
        used_identities = {row.warning_identity for row in rows}
        successful = sorted(
            (row for row in rows if row.status in self.SUCCESSFUL and row.sent_at is not None),
            key=lambda row: self._aware(row.sent_at),
            reverse=True,
        )
        latest_successful_at = self._aware(successful[0].sent_at) if successful else None
        for item in desired:
            new_next_attempt_at: datetime | None = None
            episode_identity = item["episode_identity"]
            desired_trigger = self.episode_trigger_fingerprint(item.get("payload"))

            def trigger_compatible(candidate: WarningSchedule) -> bool:
                candidate_trigger = self.episode_trigger_fingerprint(candidate.payload_json)
                # Missing fingerprints are legacy/unknown data.  Preserve
                # exact-key compatibility for them, but never merge two
                # explicitly identifiable and different trigger sources.
                return (
                    desired_trigger is None
                    or candidate_trigger is None
                    or candidate_trigger == desired_trigger
                )

            exact_episode_rows = [
                row for row in unmatched.values()
                if row.episode_identity == episode_identity
                and trigger_compatible(row)
            ]
            drift_limit = float(item.get("episode_drift_minutes", 15))

            def drift_minutes(candidate: WarningSchedule) -> float:
                return abs(
                    (self._aware(candidate.risk_time) - self._aware(item["risk_time"])).total_seconds()
                ) / 60

            fuzzy_episode_rows = []
            if desired_trigger is not None:
                fuzzy_episode_rows = [
                    row for row in unmatched.values()
                    if row.episode_identity != episode_identity
                    and drift_minutes(row) <= drift_limit
                    and self.episode_trigger_fingerprint(row.payload_json) == desired_trigger
                ]
            # Exact identity remains the primary audit key.  Nearby rows with
            # the same stable trigger augment the occurrence group so a bucket
            # boundary cannot hide a sent/failed sibling.
            same_episode_rows = exact_episode_rows + sorted(
                fuzzy_episode_rows,
                key=lambda candidate: (drift_minutes(candidate), str(candidate.id)),
            )

            success_terminal = next(
                (
                    row for row in same_episode_rows
                    if row.status in self.SUCCESSFUL
                    and drift_minutes(row) <= drift_limit
                ),
                None,
            )
            suppressed_terminal = next((
                row for row in same_episode_rows
                if row.status == "suppressed"
                and str((row.payload_json or {}).get("suppression_reason") or "") != "minimum_interval"
            ), None)
            failed_terminal = next((
                row for row in same_episode_rows
                if row.status == "failed" and drift_minutes(row) <= drift_limit
            ), None)
            active_rows = [row for row in same_episode_rows if row.status in self.ACTIVE]
            minimum_interval_rows = [
                row for row in same_episode_rows
                if row.status == "suppressed"
                and str((row.payload_json or {}).get("suppression_reason") or "") == "minimum_interval"
            ]
            candidates = active_rows or minimum_interval_rows or same_episode_rows
            candidates.sort(key=lambda row: (
                0 if row.status in self.ACTIVE or row.status == "cancelled" else 1,
                drift_minutes(row),
            ))
            blocking_terminal_sibling = bool(
                success_terminal or suppressed_terminal or failed_terminal
            )
            row = (
                success_terminal
                or suppressed_terminal
                or failed_terminal
                or (candidates[0] if candidates else None)
            )
            if row is not None:
                drift = drift_minutes(row)
                suppression_reason = str(
                    (row.payload_json or {}).get("suppression_reason") or ""
                )
                if blocking_terminal_sibling:
                    # An occurrence-scoped successful/failed delivery or a
                    # durable suppression blocks every cancelled sibling.
                    pass
                elif row.status == "suppressed" and suppression_reason == "daily_cap":
                    # The successful-send count is authoritative for the whole
                    # local day. Schedule drift can never reopen this row.
                    pass
                elif row.status == "suppressed" and suppression_reason == "minimum_interval":
                    schedule_changed = (
                        self._aware(row.target_time) != self._aware(item["target_time"])
                        or self._aware(row.risk_time) != self._aware(item["risk_time"])
                        or self._aware(row.valid_until) != self._aware(item["valid_until"])
                    )
                    next_allowed = (
                        latest_successful_at + timedelta(minutes=self.MIN_INTERVAL_MINUTES)
                        if latest_successful_at is not None else now
                    )
                    due = max(self._aware(item["target_time"]), next_allowed, now)
                    legal_new_window = (
                        schedule_changed
                        and due <= self._aware(item["valid_until"])
                        and due < self._aware(item["risk_time"])
                    )
                    if legal_new_window and len(successful) < self.MAX_DAILY_SENDS:
                        # Preserve the old suppressed audit row and create a
                        # distinct delivery opportunity below.
                        row = None
                        new_next_attempt_at = due
                # Before delivery, a moving prediction reschedules the same
                # item even when it moves substantially.  After delivery,
                # only a small drift is the same episode; a far-away risk is
                # allowed to become a new occurrence later that day.
                elif row.status not in self.ACTIVE and drift > float(
                    drift_limit
                ):
                    row = None
                elif row.status == "expired":
                    # A forecast with a genuinely future valid window is a
                    # new delivery opportunity.  Never revive the expired
                    # audit row, even when its episode identity is unchanged.
                    row = None
            if row is None:
                identity = episode_identity
                if identity in used_identities:
                    identity = hashlib.sha256(
                        "\0".join((
                            episode_identity,
                            self._aware(item["target_time"]).isoformat(),
                            self._aware(item["risk_time"]).isoformat(),
                            self._aware(item["valid_until"]).isoformat(),
                        )).encode("utf-8")
                    ).hexdigest()
                used_identities.add(identity)
                payload = dict(item["payload"])
                status = "pending"
                next_attempt_at = new_next_attempt_at or max(
                    self._aware(item["target_time"]), now
                )
                if len(successful) >= self.MAX_DAILY_SENDS:
                    status = "suppressed"
                    payload["suppression_reason"] = "daily_cap"
                    next_attempt_at = None
                session.add(WarningSchedule(
                    participant_id=participant_id, local_date=local_date,
                    forecast_id=forecast_id, forecast_version=forecast_version,
                    warning_identity=identity, target_time=item["target_time"],
                    episode_identity=episode_identity,
                    risk_time=item["risk_time"], valid_until=item["valid_until"],
                    warning_level=item["warning_level"], status=status,
                    payload_json=payload, next_attempt_at=next_attempt_at,
                ))
                counts["created"] += 1
                continue
            unmatched.pop(row.id, None)
            if row.status in {"sent", "escalated", "failed", "expired", "suppressed"}:
                # Terminal rows are immutable audit records.  A later valid
                # opportunity is represented by a new row; an ordinary drift
                # or tier change never revives or rewrites the old record.
                counts["kept"] += 1
                continue
            schedule_changed = (
                self._aware(row.target_time) != self._aware(item["target_time"])
                or self._aware(row.risk_time) != self._aware(item["risk_time"])
                or self._aware(row.valid_until) != self._aware(item["valid_until"])
            )
            changed = (
                row.forecast_version != forecast_version
                or dict(row.payload_json) != dict(item["payload"])
                or row.warning_level != item["warning_level"]
                or schedule_changed
            )
            if row.status == "claimed" and changed:
                row.status = "pending"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.next_attempt_at = max(self._aware(item["target_time"]), now)
            elif (
                row.status == "cancelled"
                and row.attempt_count == 0
                and row.sent_at is None
                and len(successful) < self.MAX_DAILY_SENDS
            ):
                # Re-entering the latest desired set is enough to reactivate
                # an untouched cancelled row; the schedule may be identical.
                due = max(self._aware(item["target_time"]), now)
                if due <= self._aware(item["valid_until"]) and due < self._aware(item["risk_time"]):
                    row.status = "pending"
                    row.next_attempt_at = due
            elif (
                schedule_changed
                and row.attempt_count == 0
                and row.status == "pending"
            ):
                # This is still the first delivery schedule, so a moved
                # forecast window replaces the old due time.  Retried rows
                # keep their delivery backoff even if the forecast changes.
                row.next_attempt_at = max(self._aware(item["target_time"]), now)
            elif schedule_changed and row.status == "delivery_unavailable":
                # Recompute the channel recheck inside the new valid window.
                # This was previously omitted, leaving a permanently active
                # row whose next attempt was outside its moved window.
                due = max(self._aware(item["target_time"]), now)
                if due >= self._aware(item["risk_time"]) or due > self._aware(item["valid_until"]):
                    row.status = "expired"
                    row.next_attempt_at = None
                else:
                    row.next_attempt_at = due
            row.forecast_id = forecast_id
            row.forecast_version = forecast_version
            row.payload_json = dict(item["payload"])
            row.target_time = item["target_time"]
            row.risk_time = item["risk_time"]
            row.valid_until = item["valid_until"]
            row.warning_level = item["warning_level"]
            row.episode_identity = item["episode_identity"]
            # A desired, untouched cancelled row is reactivated even when the
            # timestamps are identical. Other active states retain semantics.
            row.updated_at = utc_now()
            counts["rescheduled" if changed else "kept"] += 1
        for row in unmatched.values():
            if (
                row.status in self.ACTIVE
                and self._aware(row.valid_until) >= now
                and self._aware(row.risk_time) > now
            ):
                row.status = "cancelled"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.updated_at = now
                counts["cancelled"] += 1
        return counts

    @staticmethod
    def _level_rank(value: str) -> int:
        normalized = str(value).lower()
        if normalized in {"3", "red", "critical"}:
            return 3
        if normalized in {"2", "orange", "high"}:
            return 2
        return 1

    def count_successful_deliveries(
        self, participant_id: uuid.UUID, local_date: date
    ) -> int:
        with self.database.session() as session:
            return len(session.execute(select(WarningSchedule.id).where(
                WarningSchedule.participant_id == participant_id,
                WarningSchedule.local_date == local_date,
                WarningSchedule.status.in_(("sent", "escalated")),
            )).all())

    def latest_successful_delivery(
        self, participant_id: uuid.UUID, local_date: date
    ) -> datetime | None:
        with self.database.session() as session:
            row = session.execute(select(WarningSchedule).where(
                WarningSchedule.participant_id == participant_id,
                WarningSchedule.local_date == local_date,
                WarningSchedule.status.in_(("sent", "escalated")),
                WarningSchedule.sent_at.is_not(None),
            ).order_by(desc(WarningSchedule.sent_at)).limit(1)).scalar_one_or_none()
            return self._aware(row.sent_at) if row is not None else None

    def pending(self, now: datetime, *, limit: int = 100) -> list[dict[str, Any]]:
        now = self._aware(now)
        with self.database.session() as session:
            expired = session.execute(select(WarningSchedule).where(
                WarningSchedule.status.in_(("pending", "claimed")),
                or_(
                    WarningSchedule.valid_until < now,
                    WarningSchedule.risk_time <= now,
                ),
            )).scalars().all()
            for row in expired:
                row.status = "expired"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.updated_at = now
            rows = session.execute(
                select(WarningSchedule).join(
                    ForecastSnapshot, ForecastSnapshot.id == WarningSchedule.forecast_id
                ).where(
                    or_(
                        WarningSchedule.status == "pending",
                        (WarningSchedule.status == "claimed")
                        & or_(
                            WarningSchedule.lease_until.is_(None),
                            WarningSchedule.lease_until < now,
                        ),
                    ),
                    WarningSchedule.target_time <= now,
                    or_(WarningSchedule.next_attempt_at.is_(None), WarningSchedule.next_attempt_at <= now),
                    WarningSchedule.valid_until >= now,
                    WarningSchedule.risk_time > now,
                    ForecastSnapshot.valid.is_(True),
                    ForecastSnapshot.forecast_version == WarningSchedule.forecast_version,
                ).order_by(WarningSchedule.target_time).limit(limit)
            ).scalars().all()
            return [self._view(row) for row in rows]

    def mark_sent_if_current(self, warning_id: uuid.UUID, now: datetime) -> bool:
        now = self._aware(now)
        with self.database.session() as session:
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if row is None or row.status != "pending":
                return False
            if self._aware(row.valid_until) < now or self._aware(row.risk_time) <= now:
                row.status = "expired"
                row.updated_at = now
                return False
            forecast = session.get(ForecastSnapshot, row.forecast_id)
            if forecast is None or not forecast.valid or forecast.forecast_version != row.forecast_version:
                row.status = "cancelled"
                row.updated_at = utc_now()
                return False
            row.status = "sent"
            row.sent_at = now
            row.updated_at = now
            return True

    def claim_if_current(
        self, warning_id: uuid.UUID, *, now: datetime | None = None,
        lease_seconds: int = 120, max_daily_sends: int = 2,
        min_interval_minutes: int = 240,
    ) -> Optional[dict[str, Any]]:
        now = self._aware(now or utc_now())
        with self.database.session() as session:
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if row is None or row.status not in {"pending", "claimed"}:
                return None
            if row.status == "claimed" and row.lease_until and self._aware(row.lease_until) >= now:
                return None
            if self._aware(row.valid_until) < now or self._aware(row.risk_time) <= now:
                row.status = "expired"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.updated_at = now
                return None
            forecast = session.get(ForecastSnapshot, row.forecast_id)
            if forecast is None or not forecast.valid or forecast.forecast_version != row.forecast_version:
                row.status = "cancelled"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.updated_at = utc_now()
                return None
            # Serialize claims for this participant.  Counting only sent rows
            # is semantically correct, while a live claim acts as a temporary
            # reservation so two workers cannot send concurrently before the
            # first one records success.
            session.get(Participant, row.participant_id, with_for_update=True)
            live_claim = session.execute(select(WarningSchedule).where(
                WarningSchedule.participant_id == row.participant_id,
                WarningSchedule.local_date == row.local_date,
                WarningSchedule.id != row.id,
                WarningSchedule.status == "claimed",
                WarningSchedule.lease_until >= now,
            ).limit(1)).scalar_one_or_none()
            if live_claim is not None:
                row.status = "pending"
                row.next_attempt_at = max(
                    now + timedelta(seconds=1),
                    self._aware(live_claim.lease_until),
                )
                row.updated_at = now
                return None

            successful = session.execute(select(WarningSchedule).where(
                WarningSchedule.participant_id == row.participant_id,
                WarningSchedule.local_date == row.local_date,
                WarningSchedule.status.in_(("sent", "escalated")),
            ).order_by(desc(WarningSchedule.sent_at))).scalars().all()
            if len(successful) >= max(0, max_daily_sends):
                row.status = "suppressed"
                row.payload_json = {**dict(row.payload_json), "suppression_reason": "daily_cap"}
                row.next_attempt_at = None
                row.updated_at = now
                return None
            if successful and min_interval_minutes > 0:
                latest = next((item for item in successful if item.sent_at is not None), None)
                if latest is not None:
                    next_allowed = self._aware(latest.sent_at) + timedelta(
                        minutes=min_interval_minutes
                    )
                    if next_allowed > now:
                        if (
                            next_allowed < self._aware(row.risk_time)
                            and next_allowed <= self._aware(row.valid_until)
                        ):
                            row.status = "pending"
                            row.next_attempt_at = max(self._aware(row.target_time), next_allowed)
                        else:
                            row.status = "suppressed"
                            row.payload_json = {
                                **dict(row.payload_json),
                                "suppression_reason": "minimum_interval",
                            }
                            row.next_attempt_at = None
                        row.updated_at = now
                        return None
            row.status = "claimed"
            row.claimed_at = now
            row.lease_until = now + timedelta(seconds=max(1, lease_seconds))
            row.claim_token = uuid.uuid4()
            row.updated_at = now
            return self._view(row)

    def validate_claim_current(
        self, warning_id: uuid.UUID, *, claim_token: uuid.UUID | str,
        expected_forecast_version: str, now: datetime,
    ) -> bool:
        now = self._aware(now)
        token = uuid.UUID(str(claim_token))
        with self.database.session() as session:
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if (
                row is None
                or row.status != "claimed"
                or row.claim_token != token
                or row.forecast_version != expected_forecast_version
                or row.claimed_at is None
                or row.lease_until is None
                or self._aware(row.lease_until) < now
            ):
                return False
            if self._aware(row.valid_until) < now or self._aware(row.risk_time) <= now:
                row.status = "expired"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.updated_at = now
                return False
            forecast = session.get(ForecastSnapshot, row.forecast_id)
            if (
                forecast is None
                or not forecast.valid
                or forecast.forecast_version != expected_forecast_version
            ):
                row.status = "cancelled"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.updated_at = now
                return False
            return True

    def finish_claim(
        self, warning_id: uuid.UUID, *, claim_token: uuid.UUID | str,
        expected_forecast_version: str, sent: bool, now: datetime,
        retryable: bool = True, error_code: str | None = None,
        error_class: str | None = None, max_attempts: int = 5,
        retry_base_seconds: int = 60,
    ) -> bool:
        now = self._aware(now)
        token = uuid.UUID(str(claim_token))
        with self.database.session() as session:
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if (
                row is None
                or row.status != "claimed"
                or row.claim_token != token
                or row.forecast_version != expected_forecast_version
            ):
                return False
            row.attempt_count += 1
            row.last_attempt_at = now
            row.last_error_code = error_code
            row.last_error_class = error_class
            row.claimed_at = None
            row.lease_until = None
            row.claim_token = None
            if sent:
                row.status = "escalated" if bool(row.payload_json.get("escalation")) else "sent"
                row.sent_at = now
                row.next_attempt_at = None
            elif not retryable or row.attempt_count >= max_attempts:
                row.status = "failed"
                row.next_attempt_at = None
            else:
                delay = retry_base_seconds * (2 ** max(0, row.attempt_count - 1))
                next_attempt = now + timedelta(seconds=delay)
                if (
                    next_attempt >= self._aware(row.risk_time)
                    or next_attempt > self._aware(row.valid_until)
                ):
                    row.status = "expired"
                    row.next_attempt_at = None
                else:
                    row.status = "pending"
                    row.next_attempt_at = next_attempt
            row.updated_at = now
            return True

    def block_delivery(
        self, warning_id: uuid.UUID, *, claim_token: uuid.UUID | str,
        expected_forecast_version: str, now: datetime, reason: str,
    ) -> bool:
        token = uuid.UUID(str(claim_token))
        with self.database.session() as session:
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if (
                row is None
                or row.status != "claimed"
                or row.claim_token != token
                or row.forecast_version != expected_forecast_version
            ):
                return False
            now = self._aware(now)
            next_attempt = now + timedelta(minutes=5)
            if (
                next_attempt >= self._aware(row.risk_time)
                or next_attempt > self._aware(row.valid_until)
            ):
                row.status = "expired"
                row.next_attempt_at = None
            else:
                row.status = "delivery_unavailable"
                row.next_attempt_at = next_attempt
            row.last_attempt_at = now
            row.last_error_class = reason[:128]
            row.claimed_at = None
            row.lease_until = None
            row.claim_token = None
            row.updated_at = now
            return True

    def delivery_unavailable(
        self, now: datetime, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        now = self._aware(now)
        with self.database.session() as session:
            expired = session.execute(select(WarningSchedule).where(
                WarningSchedule.status == "delivery_unavailable",
                or_(
                    WarningSchedule.valid_until < now,
                    WarningSchedule.risk_time <= now,
                ),
            )).scalars().all()
            for row in expired:
                row.status = "expired"
                row.updated_at = now
            rows = session.execute(select(WarningSchedule).where(
                WarningSchedule.status == "delivery_unavailable",
                WarningSchedule.valid_until >= now,
                WarningSchedule.risk_time > now,
                WarningSchedule.next_attempt_at <= now,
            ).limit(limit)).scalars().all()
            return [self._view(row) for row in rows]

    def reactivate_delivery(
        self, warning_id: uuid.UUID, *, now: datetime, available: bool
    ) -> None:
        now = self._aware(now)
        with self.database.session() as session:
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if row is None or row.status != "delivery_unavailable":
                return
            if self._aware(row.valid_until) < now or self._aware(row.risk_time) <= now:
                row.status = "expired"
            elif available:
                row.status = "pending"
                row.next_attempt_at = now
            else:
                next_attempt = now + timedelta(minutes=5)
                if (
                    next_attempt >= self._aware(row.risk_time)
                    or next_attempt > self._aware(row.valid_until)
                ):
                    row.status = "expired"
                    row.next_attempt_at = None
                else:
                    row.next_attempt_at = next_attempt
            row.updated_at = now


class ConversationRepository:
    def __init__(self, database: Database):
        self.database = database

    def add(
        self,
        participant_id: uuid.UUID,
        role: str,
        content: str,
        *,
        feishu_message_id: Optional[str] = None,
    ) -> uuid.UUID:
        if role not in {"user", "assistant"}:
            raise ValueError("conversation role must be user or assistant")
        with self.database.session() as session:
            if feishu_message_id:
                existing = session.execute(
                    select(ConversationMessage).where(
                        ConversationMessage.participant_id == participant_id,
                        ConversationMessage.feishu_message_id == feishu_message_id,
                        ConversationMessage.role == role,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return existing.id
            row = ConversationMessage(
                participant_id=participant_id,
                feishu_message_id=feishu_message_id,
                role=role,
                content=str(content)[:8000],
            )
            session.add(row)
            session.flush()
            return row.id

    def recent(
        self,
        participant_id: uuid.UUID,
        limit: int,
        *,
        exclude_feishu_message_id: Optional[str] = None,
    ) -> list[dict[str, str]]:
        with self.database.session() as session:
            query = select(ConversationMessage).where(
                ConversationMessage.participant_id == participant_id
            )
            if exclude_feishu_message_id:
                query = query.where(
                    or_(
                        ConversationMessage.feishu_message_id.is_(None),
                        ConversationMessage.feishu_message_id
                        != exclude_feishu_message_id,
                    )
                )
            rows = list(
                session.execute(
                    query.order_by(desc(ConversationMessage.created_at))
                    .limit(max(1, min(int(limit), 100)))
                ).scalars()
            )
            rows.reverse()
            return [{"role": row.role, "content": row.content} for row in rows]


class ClaudeSessionRepository:
    """Durable one-to-one mapping between a participant and Claude session."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row: ClaudeSession) -> ClaudeSessionView:
        return ClaudeSessionView(
            participant_id=row.participant_id,
            session_id=row.session_id,
            status=row.status,
            last_message_id=row.last_message_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def get(self, participant_id: uuid.UUID) -> Optional[ClaudeSessionView]:
        with self.database.session() as session:
            row = session.get(ClaudeSession, participant_id)
            return self._view(row) if row is not None else None

    def save(
        self,
        participant_id: uuid.UUID,
        session_id: str,
        *,
        last_message_id: Optional[str],
    ) -> ClaudeSessionView:
        value = str(session_id).strip()
        if not value:
            raise ValueError("Claude session_id cannot be empty")
        with self.database.session() as session:
            row = session.get(ClaudeSession, participant_id, with_for_update=True)
            if row is None:
                row = ClaudeSession(
                    participant_id=participant_id,
                    session_id=value,
                    status="active",
                    last_message_id=last_message_id,
                )
                session.add(row)
            else:
                row.session_id = value
                row.status = "active"
                row.last_message_id = last_message_id
                row.updated_at = utc_now()
            session.flush()
            return self._view(row)

    def mark_stale(self, participant_id: uuid.UUID) -> None:
        with self.database.session() as session:
            row = session.get(ClaudeSession, participant_id, with_for_update=True)
            if row is not None:
                row.status = "stale"
                row.updated_at = utc_now()


class BotEventRepository:
    def __init__(self, database: Database):
        self.database = database

    def accept(
        self,
        event_id: str,
        message_id: Optional[str],
        participant_id: Optional[uuid.UUID],
        *,
        app_id: str,
        open_id: str,
        chat_id: str,
        chat_type: str,
        text: str,
        create_time: datetime,
    ) -> bool:
        try:
            with self.database.session() as session:
                session.add(
                    BotEvent(
                        event_id=event_id,
                        message_id=message_id,
                        app_id=app_id,
                        open_id=open_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        text=str(text)[:4000],
                        message_created_at=create_time,
                        participant_id=participant_id,
                        status="received",
                    )
                )
                session.flush()
            return True
        except IntegrityError:
            return False

    def recoverable(self, limit: int = 1000) -> list[RecoverableBotEvent]:
        """Return durable ingress that was not fully delivered before shutdown."""

        with self.database.session() as session:
            rows = session.execute(
                select(BotEvent)
                .where(BotEvent.status.in_(("received", "processing", "reply_pending")))
                .order_by(BotEvent.received_at)
                .limit(max(1, min(int(limit), 10_000)))
            ).scalars()
            return [
                RecoverableBotEvent(
                    event_id=row.event_id,
                    message_id=str(row.message_id or row.event_id),
                    app_id=row.app_id,
                    open_id=row.open_id,
                    chat_id=row.chat_id,
                    chat_type=row.chat_type,
                    text=row.text,
                    create_time=row.message_created_at,
                )
                for row in rows
            ]

    def pending_reply(self, event_id: str) -> Optional[str]:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id)
            if row is None or row.status != "reply_pending" or not row.reply_text:
                return None
            return row.reply_text

    def pending_reply_plan(self, event_id: str) -> PendingReplyPlan | None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id)
            if row is None or row.status != "reply_pending":
                return None
            raw_segments = row.reply_segments_json
            if isinstance(raw_segments, list) and raw_segments:
                segments = tuple(str(item) for item in raw_segments if str(item))
                version = str(row.reply_plan_version or "response-plan-v1")
            elif row.reply_text:
                segments = (str(row.reply_text),)
                version = "legacy-single-v1"
            else:
                return None
            next_segment = max(0, min(int(row.reply_next_segment or 0), len(segments)))
            message_ids = tuple(
                str(item) for item in (row.reply_message_ids_json or []) if item
            )
            return PendingReplyPlan(
                event_id=row.event_id,
                full_text=str(row.reply_text or "\n\n".join(segments)),
                segments=segments,
                next_segment=next_segment,
                message_ids=message_ids,
                plan_version=version,
            )

    def stage_reply_plan(
        self,
        event_id: str,
        *,
        full_text: str,
        segments: list[str] | tuple[str, ...],
        plan_version: str = "response-plan-v1",
    ) -> None:
        normalized = [str(item) for item in segments if str(item)]
        if not normalized:
            raise ValueError("reply plan must contain at least one segment")
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is None:
                return
            if row.status == "reply_pending" and row.reply_segments_json:
                return
            row.reply_text = str(full_text)
            row.reply_segments_json = normalized
            row.reply_next_segment = 0
            row.reply_message_ids_json = []
            row.reply_plan_version = str(plan_version)[:32]
            row.status = "reply_pending"
            row.error_code = None

    def mark_reply_segment_sent(
        self,
        event_id: str,
        *,
        segment_index: int,
        message_id: str,
    ) -> None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is None or row.status != "reply_pending":
                return
            current = int(row.reply_next_segment or 0)
            if current > segment_index:
                return
            if current != segment_index:
                raise ValueError("reply segments must be recorded in order")
            message_ids = list(row.reply_message_ids_json or [])
            if len(message_ids) == segment_index:
                message_ids.append(str(message_id)[:128])
            elif len(message_ids) > segment_index:
                message_ids[segment_index] = str(message_id)[:128]
            else:
                raise ValueError("reply message id history is inconsistent")
            row.reply_message_ids_json = message_ids
            row.reply_next_segment = segment_index + 1
            row.reply_message_id = str(message_id)[:128]
            row.error_code = None

    def finish_reply_plan(self, event_id: str) -> None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is None:
                return
            segments = row.reply_segments_json or (
                [row.reply_text] if row.reply_text else []
            )
            if int(row.reply_next_segment or 0) < len(segments):
                raise ValueError("reply plan is not fully delivered")
            row.status = "completed"
            row.error_code = None
            row.processed_at = utc_now()

    def cancel_reply_plan(self, event_id: str) -> None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is None or row.status == "completed":
                return
            row.status = "interrupted"
            row.error_code = "stopped"
            row.processed_at = utc_now()

    def stage_reply(self, event_id: str, text: str) -> None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is None:
                return
            row.reply_text = str(text)[:4000]
            row.reply_segments_json = None
            row.reply_next_segment = 0
            row.reply_message_ids_json = None
            row.reply_plan_version = None
            row.status = "reply_pending"
            row.error_code = None

    def note_reply_failure(self, event_id: str) -> None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is not None:
                row.status = "reply_pending"
                row.error_code = "send_failed"

    def set_processing(self, event_id: str, participant_id: Optional[uuid.UUID]) -> None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is None:
                return
            row.participant_id = participant_id
            row.status = "processing"
            row.attempts += 1

    def assign_participant(self, event_id: str, participant_id: uuid.UUID) -> None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is not None:
                row.participant_id = participant_id

    def finish(
        self,
        event_id: str,
        *,
        status: str,
        error_code: Optional[str] = None,
        reply_message_id: Optional[str] = None,
    ) -> None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is None:
                return
            row.status = status
            row.error_code = str(error_code)[:64] if error_code else None
            if reply_message_id:
                row.reply_message_id = str(reply_message_id)[:128]
            row.processed_at = utc_now()

    def save_telemetry(self, event_id: str, metrics: dict[str, Any]) -> None:
        """Persist non-secret delivery timings in an independent transaction."""

        allowed = {
            "latency_ms",
            "received_to_agent_start_ms",
            "agent_start_to_first_activity_ms",
            "first_tool_start_ms",
            "tool_duration_ms",
            "agent_result_ms",
            "presentation_ms",
            "card_upload_ms",
            "first_final_send_ms",
            "total_delivery_ms",
            "segment_count",
            "presentation_agent_used",
        }
        payload = {key: metrics[key] for key in allowed if key in metrics}
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is not None:
                row.telemetry_json = payload


class RuntimeIncidentRepository:
    def __init__(self, database: Database):
        self.database = database

    def record(
        self,
        *,
        severity: str,
        subsystem: str,
        event_name: str,
        summary: str,
        participant_id: uuid.UUID | None = None,
        bot_event_id: str | None = None,
        error_code: str | None = None,
        error_class: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        row = RuntimeIncident(
            severity=str(severity)[:16],
            subsystem=str(subsystem)[:64],
            event_name=str(event_name)[:128],
            participant_id=participant_id,
            bot_event_id=str(bot_event_id)[:128] if bot_event_id else None,
            error_code=str(error_code)[:128] if error_code else None,
            error_class=str(error_class)[:128] if error_class else None,
            summary=str(summary)[:1000],
            details_json=dict(details or {}),
        )
        with self.database.session() as session:
            session.add(row)
            session.flush()
            return row.id


class AgentRunRepository:
    def __init__(self, database: Database):
        self.database = database

    def start(
        self,
        participant_id: uuid.UUID,
        message_id: Optional[str],
        model: str,
        skill_version: str,
    ) -> uuid.UUID:
        with self.database.session() as session:
            row = AgentRun(
                participant_id=participant_id,
                message_id=message_id,
                model=model,
                skill_version=skill_version,
                status="running",
                started_at=utc_now(),
            )
            session.add(row)
            session.flush()
            return row.id

    def finish(self, run_id: uuid.UUID, status: str) -> None:
        with self.database.session() as session:
            row = session.get(AgentRun, run_id, with_for_update=True)
            if row is not None:
                row.status = status
                row.finished_at = utc_now()

    def tool_call(
        self,
        run_id: uuid.UUID,
        tool_name: str,
        arguments: Optional[dict[str, Any]],
        result: Optional[dict[str, Any]],
        status: str,
    ) -> None:
        with self.database.session() as session:
            session.add(
                AgentToolCall(
                    agent_run_id=run_id,
                    tool_name=str(tool_name)[:128],
                    arguments_summary_json=arguments,
                    result_summary_json=result,
                    status=status,
                )
            )
