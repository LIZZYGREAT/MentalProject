"""Participant-scoped repositories. Every user query takes a trusted UUID."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import logging
import math
import uuid
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import case, desc, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError


logger = logging.getLogger(__name__)


def _integrity_matches(
    exc: IntegrityError,
    *,
    constraint_names: set[str],
    sqlite_columns: tuple[str, ...],
) -> bool:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name is not None:
        return str(constraint_name) in constraint_names
    message = str(original or exc).casefold()
    if any(name.casefold() in message for name in constraint_names):
        return True
    return all(column.casefold() in message for column in sqlite_columns)

from app.db import Database
from app.contracts.warning import WarningDeliveryPolicyConfig
from app.contracts.research import (
    EVENT_APPRAISAL_SCORE_FIELDS,
    SLOW_STATE_FIELDS,
    aware_utc,
    normalize_instrument_name,
    score_0_10,
    validate_profile_v2,
)
from app.services.same_day_late_care_policy import SameDayLateCarePolicy
from app.models import (
    AgentRun,
    AgentToolCall,
    BotEvent,
    CalendarSnapshot,
    ClaudeSession,
    ConversationMessage,
    EventAppraisalFeedback,
    EventSemanticCache,
    FeishuBinding,
    FeishuOAuthToken,
    Participant,
    ParticipantCarePreference,
    ParticipantProfile,
    ParticipantSlowState,
    PsychometricAssessment,
    LearnedModelProfile,
    ForecastCurrentnessEvent,
    ForecastSnapshot,
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


class ForecastInputChangedError(RuntimeError):
    """The inputs used by a forecast changed before it could be committed."""

    def __init__(self, input_name: str):
        self.input_name = input_name
        super().__init__(f"{input_name} revision changed before forecast save")


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
        validated = validate_profile_v2(profile)
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
                    profile_json=validated,
                )
            )
            return version


class LearnedProfileRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row: LearnedModelProfile) -> dict[str, Any]:
        uncertainty = dict(row.uncertainty_json or {})
        return {
            "version": row.version,
            "parameters": dict(row.parameters_json),
            "uncertainty": uncertainty,
            "source": row.source,
            "model_version": row.model_version,
            "validation_status": row.validation_status,
            "sample_count": row.sample_count,
            "day_count": row.day_count,
            "confidence": row.confidence,
            "window_start": row.window_start.isoformat(),
            "window_end": row.window_end.isoformat(),
            "created_at": row.created_at.isoformat(),
        }

    def latest(self, participant_id: uuid.UUID) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.execute(select(LearnedModelProfile).where(
                LearnedModelProfile.participant_id == participant_id
            ).order_by(desc(LearnedModelProfile.version)).limit(1)).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def runtime_active(
        self, participant_id: uuid.UUID
    ) -> Optional[dict[str, Any]]:
        """Return only parameters allowed to affect production runtime.

        Rows migrated from the pre-validation schema carry model_version=legacy
        and candidate status. They remain active for behavior compatibility,
        without falsely claiming that they completed formal validation.
        """

        with self.database.session() as session:
            row = session.execute(
                select(LearnedModelProfile)
                .where(
                    LearnedModelProfile.participant_id == participant_id,
                    or_(
                        LearnedModelProfile.validation_status == "validated",
                        (
                            (LearnedModelProfile.validation_status == "candidate")
                            & (LearnedModelProfile.model_version == "legacy")
                        ),
                    ),
                )
                .order_by(desc(LearnedModelProfile.version))
                .limit(1)
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def current(self, participant_id: uuid.UUID) -> Optional[dict[str, Any]]:
        """Compatibility alias for research callers; prefer latest()."""

        return self.latest(participant_id)

    @staticmethod
    def _valid_uncertainty(
        parameters: Mapping[str, Any], uncertainty: Mapping[str, Any]
    ) -> bool:
        if not parameters or not uncertainty:
            return False
        if set(parameters) - set(uncertainty):
            return False

        def has_finite_number(value: Any) -> bool:
            if isinstance(value, bool):
                return False
            if isinstance(value, (int, float)):
                return float(value) >= 0 and math.isfinite(float(value))
            if isinstance(value, Mapping):
                std_error = value.get("std_error")
                if std_error is not None:
                    if isinstance(std_error, bool):
                        return False
                    try:
                        if float(std_error) < 0 or not math.isfinite(float(std_error)):
                            return False
                    except (TypeError, ValueError):
                        return False
                return bool(value) and any(
                    has_finite_number(child) for child in value.values()
                )
            if isinstance(value, (list, tuple)):
                return bool(value) and any(has_finite_number(child) for child in value)
            return False

        return all(has_finite_number(uncertainty[name]) for name in parameters)

    def save(
        self, participant_id: uuid.UUID, *, parameters: dict[str, Any],
        sample_count: int, day_count: int, confidence: float,
        window_start: date, window_end: date, source: str = "calibration.v1",
        uncertainty: dict[str, Any] | None = None,
        model_version: str = "mindflow-ctssm-runtime-v7",
        validation_status: str = "candidate",
    ) -> dict[str, Any]:
        if validation_status not in {"candidate", "validated", "rejected"}:
            raise ValueError("invalid validation_status")
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count < 0
        ):
            raise ValueError("sample_count must be a non-negative integer")
        if (
            isinstance(day_count, bool)
            or not isinstance(day_count, int)
            or day_count < 0
        ):
            raise ValueError("day_count must be a non-negative integer")
        if (
            not isinstance(window_start, date)
            or isinstance(window_start, datetime)
            or not isinstance(window_end, date)
            or isinstance(window_end, datetime)
        ):
            raise ValueError("window_start and window_end must be dates")
        if window_start > window_end:
            raise ValueError("window_start must be on or before window_end")
        normalized_source = str(source or "").strip()
        normalized_model_version = str(model_version or "").strip()
        if not normalized_source:
            raise ValueError("source is required")
        if not normalized_model_version:
            raise ValueError("model_version is required")
        if normalized_model_version == "legacy":
            raise ValueError(
                "model_version='legacy' is reserved for migrated "
                "pre-validation profiles"
            )
        if isinstance(confidence, bool):
            raise ValueError("confidence must be between 0 and 1")
        try:
            normalized_confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be between 0 and 1") from exc
        if (
            not math.isfinite(normalized_confidence)
            or not 0.0 <= normalized_confidence <= 1.0
        ):
            raise ValueError("confidence must be between 0 and 1")
        if not isinstance(parameters, Mapping):
            raise ValueError("parameters must be an object")
        if uncertainty is not None and not isinstance(uncertainty, Mapping):
            raise ValueError("uncertainty must be an object")
        normalized_uncertainty = dict(uncertainty or {})
        if validation_status == "validated" and not self._valid_uncertainty(
            parameters, normalized_uncertainty
        ):
            raise ValueError(
                "validated parameters require uncertainty for every parameter"
            )
        with self.database.session() as session:
            if (
                session.get(Participant, participant_id, with_for_update=True)
                is None
            ):
                raise ValueError("participant not found")
            latest = session.execute(select(LearnedModelProfile.version).where(
                LearnedModelProfile.participant_id == participant_id
            ).order_by(desc(LearnedModelProfile.version)).limit(1)).scalar_one_or_none()
            row = LearnedModelProfile(
                participant_id=participant_id, version=int(latest or 0) + 1,
                parameters_json=dict(parameters),
                uncertainty_json=normalized_uncertainty,
                source=normalized_source[:64],
                model_version=normalized_model_version[:64],
                validation_status=validation_status,
                sample_count=sample_count, day_count=day_count,
                confidence=normalized_confidence,
                window_start=window_start, window_end=window_end,
            )
            session.add(row)
            session.flush()
            return self._view(row)


class PsychometricAssessmentRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row: PsychometricAssessment) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "instrument_name": row.instrument_name,
            "instrument_version": row.instrument_version,
            "language": row.language,
            "raw_items": dict(row.raw_items_json),
            "scores": dict(row.scores_json),
            "administered_at": row.administered_at.isoformat(),
            "reference_period": row.reference_period,
            "created_at": row.created_at.isoformat(),
        }

    def record(
        self,
        participant_id: uuid.UUID,
        *,
        instrument_name: str,
        instrument_version: str,
        language: str,
        raw_items: dict[str, Any],
        scores: dict[str, Any],
        administered_at: datetime,
        reference_period: str | None = None,
    ) -> dict[str, Any]:
        name = normalize_instrument_name(instrument_name)
        version = str(instrument_version or "").strip()
        locale = str(language or "").strip()
        if not version or not locale:
            raise ValueError("instrument_version and language are required")
        if not isinstance(raw_items, dict) or not isinstance(scores, dict):
            raise ValueError("raw_items and scores must be objects")
        with self.database.session() as session:
            if session.get(Participant, participant_id) is None:
                raise ValueError("participant not found")
            row = PsychometricAssessment(
                participant_id=participant_id,
                instrument_name=name,
                instrument_version=version[:32],
                language=locale[:16],
                raw_items_json=dict(raw_items),
                scores_json=dict(scores),
                administered_at=aware_utc(administered_at, "administered_at"),
                reference_period=(
                    str(reference_period).strip()[:64]
                    if reference_period is not None else None
                ),
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def history(self, participant_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(PsychometricAssessment)
                .where(PsychometricAssessment.participant_id == participant_id)
                .order_by(desc(PsychometricAssessment.administered_at))
                .limit(max(1, min(limit, 500)))
            ).scalars().all()
            return [self._view(row) for row in rows]

    def latest_by_instrument(
        self, participant_id: uuid.UUID, instrument_name: str
    ) -> Optional[dict[str, Any]]:
        name = normalize_instrument_name(instrument_name)
        with self.database.session() as session:
            row = session.execute(
                select(PsychometricAssessment)
                .where(
                    PsychometricAssessment.participant_id == participant_id,
                    PsychometricAssessment.instrument_name == name,
                )
                .order_by(
                    desc(PsychometricAssessment.administered_at),
                    desc(PsychometricAssessment.created_at),
                )
                .limit(1)
            ).scalar_one_or_none()
            return self._view(row) if row is not None else None


class ParticipantSlowStateRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(row: ParticipantSlowState) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "effective_at": row.effective_at.isoformat(),
            "cadence": row.cadence,
            "source": row.source,
            **{name: getattr(row, name) for name in SLOW_STATE_FIELDS},
            "created_at": row.created_at.isoformat(),
        }

    def record(
        self,
        participant_id: uuid.UUID,
        *,
        effective_at: datetime,
        cadence: str,
        source: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        cadence_value = str(cadence).strip().lower()
        if cadence_value not in {"daily", "weekly"}:
            raise ValueError("cadence must be daily or weekly")
        unknown = set(values) - set(SLOW_STATE_FIELDS)
        if unknown:
            raise ValueError(f"unsupported slow-state fields: {sorted(unknown)}")
        normalized = dict(values)
        for name in (
            "rolling_7d_stress",
            "rolling_7d_workload",
            "rolling_7d_energy",
            "recent_recovery_quality",
        ):
            if normalized.get(name) is not None:
                normalized[name] = score_0_10(normalized[name], name)
        if normalized.get("recent_sleep_debt") is not None:
            debt = float(normalized["recent_sleep_debt"])
            if not 0.0 <= debt <= 24.0:
                raise ValueError("recent_sleep_debt must be between 0 and 24 hours")
            normalized["recent_sleep_debt"] = debt
        if normalized.get("exam_period_flag") is not None and not isinstance(
            normalized["exam_period_flag"], bool
        ):
            raise ValueError("exam_period_flag must be boolean")
        with self.database.session() as session:
            if session.get(Participant, participant_id) is None:
                raise ValueError("participant not found")
            row = ParticipantSlowState(
                participant_id=participant_id,
                effective_at=aware_utc(effective_at, "effective_at"),
                cadence=cadence_value,
                source=str(source or "").strip()[:64],
                **normalized,
            )
            if not row.source:
                raise ValueError("source is required")
            session.add(row)
            session.flush()
            return self._view(row)

    def history(self, participant_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(ParticipantSlowState)
                .where(ParticipantSlowState.participant_id == participant_id)
                .order_by(desc(ParticipantSlowState.effective_at))
                .limit(max(1, min(limit, 500)))
            ).scalars().all()
            return [self._view(row) for row in rows]


class EventAppraisalFeedbackRepository:
    def __init__(self, database: Database):
        self.database = database
        self.forecasts = ForecastSnapshotRepository(database)

    @staticmethod
    def _view(row: EventAppraisalFeedback) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "event_id": row.event_id,
            **{name: getattr(row, name) for name in EVENT_APPRAISAL_SCORE_FIELDS},
            "event_type": row.event_type,
            "course_name": row.course_name,
            "workload_feature_vector": row.workload_feature_vector,
            "workload_prior": row.workload_prior,
            "observed_workload": row.observed_workload,
            "workload_residual": row.workload_residual,
            "event_local_date": (
                row.event_local_date.isoformat() if row.event_local_date else None
            ),
            "event_start_at": (
                row.event_start_at.isoformat() if row.event_start_at else None
            ),
            "source_forecast_id": (
                str(row.source_forecast_id) if row.source_forecast_id else None
            ),
            "source_forecast_version": row.source_forecast_version,
            "source_semantic_revision": row.source_semantic_revision,
            "workload_schema_version": row.workload_schema_version,
            "workload_model_version": row.workload_model_version,
            "submitted_at": row.submitted_at.isoformat(),
            "created_at": row.created_at.isoformat(),
        }

    def record(
        self,
        participant_id: uuid.UUID,
        *,
        event_id: str,
        submitted_at: datetime,
        event_local_date: date | None = None,
        event_start_at: datetime | None = None,
        **scores: Any,
    ) -> dict[str, Any]:
        knowledge_time = utc_now()
        event = str(event_id or "").strip()
        if not event:
            raise ValueError("event_id is required")
        missing = set(EVENT_APPRAISAL_SCORE_FIELDS) - set(scores)
        unknown = set(scores) - set(EVENT_APPRAISAL_SCORE_FIELDS)
        if missing or unknown:
            raise ValueError(
                f"event appraisal fields mismatch; missing={sorted(missing)}, "
                f"unknown={sorted(unknown)}"
            )
        normalized = {
            name: score_0_10(scores[name], name)
            for name in EVENT_APPRAISAL_SCORE_FIELDS
        }
        from services.workload import (
            WORKLOAD_FEATURE_NAMES,
            observed_workload,
        )

        submitted_utc = aware_utc(submitted_at, "submitted_at")
        start_utc = (
            aware_utc(event_start_at, "event_start_at")
            if event_start_at is not None
            else None
        )
        if (event_local_date is None) != (start_utc is None):
            raise ValueError("event_local_date and event_start_at must be supplied together")

        # Workload features are model-owned.  Resolve them only from the
        # forecast that was actually current at the earliest causal instant;
        # never scan newer snapshots or accept a client echo as provenance.
        causal_forecast = None
        semantic_context: dict[str, Any] = {}
        presentation: Mapping[str, Any] | None = None
        if event_local_date is not None and start_utc is not None:
            causal_forecast = self.forecasts.current_at(
                participant_id,
                event_local_date,
                min(start_utc, submitted_utc, knowledge_time),
            )
            if causal_forecast is not None:
                semantic_item = next(
                    (
                        item
                        for item in list(causal_forecast.get("semantic_input") or [])
                        if str(item.get("event_id") or "") == event
                    ),
                    None,
                )
                presentation = next(
                    (
                        item
                        for item in list(
                            (causal_forecast.get("output") or {}).get(
                                "classified_calendar_events"
                            )
                            or []
                        )
                        if str(item.get("id") or item.get("event_id") or "") == event
                    ),
                    None,
                )
                if isinstance(semantic_item, Mapping) and isinstance(
                    semantic_item.get("semantic"), Mapping
                ):
                    semantic_context = dict(semantic_item["semantic"])

        def workload_unit(value: Any, name: str) -> float:
            if isinstance(value, bool):
                raise ValueError(f"{name} must be between 0 and 1")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be between 0 and 1") from exc
            if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
            return number

        feature_vector = None
        prior = None
        try:
            workload_feature_vector = semantic_context.get(
                "workload_feature_vector"
            )
            if not isinstance(workload_feature_vector, Mapping) or not all(
                name in workload_feature_vector for name in WORKLOAD_FEATURE_NAMES
            ):
                raise ValueError("workload feature vector is incomplete")
            feature_vector = {
                name: workload_unit(workload_feature_vector[name], name)
                for name in WORKLOAD_FEATURE_NAMES
            }
            prior = workload_unit(
                semantic_context.get("workload_prior"), "workload_prior"
            )
        except (KeyError, TypeError, ValueError):
            # Appraisal feedback is primary evidence.  A malformed model-owned
            # context must never prevent its persistence or create partial
            # provenance that could later be mistaken for a reproducible prior.
            causal_forecast = None
            semantic_context = {}
            presentation = None
            feature_vector = None
            prior = None
        observed = observed_workload(normalized)
        with self.database.session() as session:
            if session.get(Participant, participant_id) is None:
                raise ValueError("participant not found")
            row = EventAppraisalFeedback(
                participant_id=participant_id,
                event_id=event[:256],
                submitted_at=submitted_utc,
                created_at=knowledge_time,
                event_local_date=event_local_date,
                event_start_at=start_utc,
                event_type=(
                    str((presentation or {}).get("event_type") or "").strip()[:32]
                    or None
                ),
                course_name=(
                    str(
                        (presentation or {}).get("related_course_name")
                        or (presentation or {}).get("course_name")
                        or ""
                    ).strip()[:200]
                    or None
                ),
                workload_feature_vector=feature_vector,
                workload_prior=prior,
                observed_workload=observed,
                workload_residual=(observed - prior) if prior is not None else None,
                source_forecast_id=(
                    uuid.UUID(str(causal_forecast["id"])) if causal_forecast else None
                ),
                source_forecast_version=(
                    str(causal_forecast.get("forecast_version") or "")[:64] or None
                    if causal_forecast
                    else None
                ),
                source_semantic_revision=(
                    str(causal_forecast.get("semantic_revision") or "")[:64] or None
                    if causal_forecast
                    else None
                ),
                workload_schema_version=(
                    str(
                        semantic_context.get("workload_schema_version")
                        or (causal_forecast.get("output") or {}).get(
                            "workload_schema_version"
                        )
                        or ""
                    )[:64]
                    or None
                    if causal_forecast
                    else None
                ),
                workload_model_version=(
                    str(
                        semantic_context.get("workload_model_version")
                        or (causal_forecast.get("output") or {}).get(
                            "workload_model_version"
                        )
                        or ""
                    )[:64]
                    or None
                    if causal_forecast
                    else None
                ),
                **normalized,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def history(self, participant_id: uuid.UUID, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(EventAppraisalFeedback)
                .where(EventAppraisalFeedback.participant_id == participant_id)
                .order_by(desc(EventAppraisalFeedback.submitted_at))
                .limit(max(1, min(limit, 500)))
            ).scalars().all()
            return [self._view(row) for row in rows]


@dataclass(frozen=True)
class ObservationWriteResult:
    observation_id: uuid.UUID
    observed_at: datetime
    created: bool
    persisted_payload: dict[str, Any]
    idempotency_conflict: bool = False


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
        return self.add_with_status(
            participant_id,
            observation_type,
            payload,
            observed_at=observed_at,
            source_message_id=source_message_id,
        ).observation_id

    def add_with_status(
        self,
        participant_id: uuid.UUID,
        observation_type: str,
        payload: dict[str, Any],
        *,
        observed_at: Optional[datetime] = None,
        source_message_id: Optional[str] = None,
    ) -> "ObservationWriteResult":
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
                # Participant is the shared lock root for observations,
                # forecast invalidation, and forecast publication.
                session.get(Participant, participant_id, with_for_update=True)
                existing = find_existing(session)
                if existing is not None:
                    persisted_payload = dict(existing.payload_json)
                    return ObservationWriteResult(
                        observation_id=existing.id,
                        observed_at=self._aware(existing.observed_at),
                        created=False,
                        persisted_payload=persisted_payload,
                        idempotency_conflict=(
                            _canonical_json(persisted_payload)
                            != _canonical_json(dict(payload))
                        ),
                    )
                timestamp = observed_at or utc_now()
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                else:
                    timestamp = timestamp.astimezone(timezone.utc)
                row = StateObservation(
                    participant_id=participant_id,
                    observation_type=observation_type,
                    source_message_id=source_message_id,
                    payload_json=dict(payload),
                    observed_at=timestamp,
                )
                session.add(row)
                session.flush()
                return ObservationWriteResult(
                    observation_id=row.id,
                    observed_at=self._aware(row.observed_at),
                    created=True,
                    persisted_payload=dict(row.payload_json),
                )
        except IntegrityError:
            # A competing request can commit after the SELECT but before this
            # INSERT. The database constraint is authoritative; return the row
            # that won the race instead of turning an idempotent retry into 500.
            with self.database.session() as session:
                existing = find_existing(session)
                if existing is not None:
                    persisted_payload = dict(existing.payload_json)
                    return ObservationWriteResult(
                        observation_id=existing.id,
                        observed_at=self._aware(existing.observed_at),
                        created=False,
                        persisted_payload=persisted_payload,
                        idempotency_conflict=(
                            _canonical_json(persisted_payload)
                            != _canonical_json(dict(payload))
                        ),
                    )
            raise

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

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
            return [self._view(row) for row in rows]

    def recent_before(
        self,
        participant_id: uuid.UUID,
        *,
        before: datetime,
        max_age: timedelta,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return observations in the closed interval ``before-max_age..before``."""

        cutoff = self._aware(before)
        age = max(timedelta(0), max_age)
        with self.database.session() as session:
            rows = session.execute(
                select(StateObservation)
                .where(
                    StateObservation.participant_id == participant_id,
                    StateObservation.observed_at >= cutoff - age,
                    StateObservation.observed_at <= cutoff,
                )
                .order_by(
                    desc(StateObservation.observed_at),
                    desc(StateObservation.created_at),
                    desc(StateObservation.id),
                )
                .limit(max(1, min(int(limit), 100)))
            ).scalars()
            return [self._view(row) for row in rows]

    def for_local_date(
        self,
        participant_id: uuid.UUID,
        local_date: date,
        *,
        timezone_name: str,
        as_of: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return only observations whose timestamp belongs to one local day."""

        timezone_value = ZoneInfo(timezone_name)
        day_start = datetime.combine(local_date, time.min, timezone_value).astimezone(
            timezone.utc
        )
        day_end = datetime.combine(
            local_date + timedelta(days=1), time.min, timezone_value
        ).astimezone(timezone.utc)
        conditions = [
            StateObservation.participant_id == participant_id,
            StateObservation.observed_at >= day_start,
            StateObservation.observed_at < day_end,
        ]
        if as_of is not None:
            cutoff = (
                as_of.replace(tzinfo=timezone.utc)
                if as_of.tzinfo is None
                else as_of.astimezone(timezone.utc)
            )
            conditions.append(StateObservation.observed_at <= cutoff)
            conditions.append(StateObservation.created_at <= cutoff)
        with self.database.session() as session:
            rows = session.execute(
                select(StateObservation)
                .where(*conditions)
                .order_by(
                    desc(StateObservation.observed_at),
                    desc(StateObservation.created_at),
                    desc(StateObservation.id),
                )
                .limit(max(1, min(int(limit), 500)))
            ).scalars()
            return [self._view(row) for row in rows]

    @staticmethod
    def _view(row: StateObservation) -> dict[str, Any]:
        def utc_iso(value: datetime) -> str:
            aware = (
                value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value.astimezone(timezone.utc)
            )
            return aware.isoformat()

        return {
            "id": str(row.id),
            "type": row.observation_type,
            "payload": dict(row.payload_json),
            "observed_at": utc_iso(row.observed_at),
            "created_at": utc_iso(row.created_at),
        }


class CalendarSnapshotRepository:
    CURRENT = "current"
    PROVIDER_DEGRADED = "provider_degraded"
    MUTATION_REFRESH_PENDING = "mutation_refresh_pending"

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
            "snapshot_state": row.snapshot_state,
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

    @staticmethod
    def _matches_expected(
        row: CalendarSnapshot | None,
        *,
        expected_snapshot_id: str | None,
        expected_revision: str | None,
        expected_state: str | None,
    ) -> bool:
        if expected_snapshot_id is None:
            return row is None
        return bool(
            row is not None
            and str(row.id) == str(expected_snapshot_id)
            and row.calendar_revision == expected_revision
            and row.snapshot_state == expected_state
        )

    def commit_provider_read(
        self,
        participant_id: uuid.UUID,
        local_date: date,
        *,
        expected_snapshot_id: str | None,
        expected_revision: str | None,
        expected_state: str | None,
        revision: str,
        events: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Commit a provider response only if its read fence is still current."""

        with self.database.session() as session:
            # Calendar mutation invalidation uses the same participant-level
            # serialization root and then locks CalendarSnapshot.
            session.get(Participant, participant_id, with_for_update=True)
            row = session.execute(
                select(CalendarSnapshot).where(
                    CalendarSnapshot.participant_id == participant_id,
                    CalendarSnapshot.local_date == local_date,
                ).with_for_update()
            ).scalar_one_or_none()
            if not self._matches_expected(
                row,
                expected_snapshot_id=expected_snapshot_id,
                expected_revision=expected_revision,
                expected_state=expected_state,
            ):
                raise ForecastInputChangedError("calendar")

            now = utc_now()
            changed = row is None or row.calendar_revision != revision
            if row is None:
                row = CalendarSnapshot(
                    participant_id=participant_id,
                    local_date=local_date,
                    calendar_revision=revision,
                    events_json=list(events),
                    degraded=False,
                    snapshot_state=self.CURRENT,
                    last_refresh_attempt_at=now,
                    last_refresh_success_at=now,
                    last_refresh_error_class=None,
                )
                session.add(row)
            else:
                row.calendar_revision = revision
                row.events_json = list(events)
                row.degraded = False
                row.snapshot_state = self.CURRENT
                row.last_refresh_attempt_at = now
                row.last_refresh_success_at = now
                row.last_refresh_error_class = None
                row.updated_at = now
            session.flush()
            return self._view(row), changed

    def commit_provider_failure(
        self,
        participant_id: uuid.UUID,
        local_date: date,
        *,
        expected_snapshot_id: str | None,
        expected_revision: str | None,
        expected_state: str | None,
        error_class: str,
        empty_revision: str,
    ) -> tuple[dict[str, Any], bool]:
        """Record a provider failure without crossing a newer read fence."""

        with self.database.session() as session:
            session.get(Participant, participant_id, with_for_update=True)
            row = session.execute(
                select(CalendarSnapshot).where(
                    CalendarSnapshot.participant_id == participant_id,
                    CalendarSnapshot.local_date == local_date,
                ).with_for_update()
            ).scalar_one_or_none()
            if not self._matches_expected(
                row,
                expected_snapshot_id=expected_snapshot_id,
                expected_revision=expected_revision,
                expected_state=expected_state,
            ):
                raise ForecastInputChangedError("calendar")

            now = utc_now()
            if row is None:
                row = CalendarSnapshot(
                    participant_id=participant_id,
                    local_date=local_date,
                    calendar_revision=empty_revision,
                    events_json=[],
                    degraded=True,
                    snapshot_state=self.PROVIDER_DEGRADED,
                    last_refresh_attempt_at=now,
                    last_refresh_success_at=None,
                    last_refresh_error_class=str(error_class)[:128],
                )
                session.add(row)
            else:
                # A read-back that started from mutation_refresh_pending may
                # record diagnostics, but cannot make retained events usable.
                if row.snapshot_state != self.MUTATION_REFRESH_PENDING:
                    row.snapshot_state = self.PROVIDER_DEGRADED
                    row.degraded = True
                row.last_refresh_attempt_at = now
                row.last_refresh_error_class = str(error_class)[:128]
                row.updated_at = now
            session.flush()
            return self._view(row), False

class EventSemanticCacheRepository:
    def __init__(self, database: Database):
        self.database = database

    def get_entry(
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
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "status": str(row.status),
                "assessment": dict(row.assessment_json),
            }

    def get(
        self, participant_id: uuid.UUID, fingerprint: str, *,
        schema_version: str, prompt_version: str, model: str,
    ) -> Optional[dict[str, Any]]:
        """Backward-compatible complete-only cache read."""

        entry = self.get_entry(
            participant_id,
            fingerprint,
            schema_version=schema_version,
            prompt_version=prompt_version,
            model=model,
        )
        if entry is None or entry["status"] != "complete":
            return None
        return dict(entry["assessment"])

    def put(
        self, participant_id: uuid.UUID, fingerprint: str, assessment: dict[str, Any], *,
        schema_version: str, prompt_version: str, model: str,
    ) -> None:
        self.put_complete(
            participant_id,
            fingerprint,
            assessment,
            schema_version=schema_version,
            prompt_version=prompt_version,
            model=model,
        )

    def put_complete(
        self, participant_id: uuid.UUID, fingerprint: str, assessment: dict[str, Any], *,
        schema_version: str, prompt_version: str, model: str,
    ) -> None:
        self._put(
            participant_id,
            fingerprint,
            assessment,
            status="complete",
            schema_version=schema_version,
            prompt_version=prompt_version,
            model=model,
        )

    def put_rejected(
        self,
        participant_id: uuid.UUID,
        fingerprint: str,
        *,
        reason: str,
        confidence: float | None,
        schema_version: str,
        prompt_version: str,
        model: str,
        assessment: dict[str, Any] | None = None,
    ) -> None:
        rejection: dict[str, Any] = {"reason": str(reason)[:128]}
        if confidence is not None:
            rejection["confidence"] = float(confidence)
        payload = dict(assessment or {})
        payload["rejection"] = rejection
        self._put(
            participant_id,
            fingerprint,
            payload,
            status="rejected",
            schema_version=schema_version,
            prompt_version=prompt_version,
            model=model,
        )

    def put_partial(
        self,
        participant_id: uuid.UUID,
        fingerprint: str,
        assessment: dict[str, Any],
        *,
        schema_version: str,
        prompt_version: str,
        model: str,
    ) -> None:
        self._put(
            participant_id,
            fingerprint,
            assessment,
            status="partial",
            schema_version=schema_version,
            prompt_version=prompt_version,
            model=model,
        )

    def _put(
        self,
        participant_id: uuid.UUID,
        fingerprint: str,
        assessment: dict[str, Any],
        *,
        status: str,
        schema_version: str,
        prompt_version: str,
        model: str,
    ) -> None:
        if status not in {"complete", "partial", "rejected"}:
            raise ValueError("unsupported semantic cache status")
        now = utc_now()
        values = {
            "id": uuid.uuid4(),
            "participant_id": participant_id,
            "fingerprint": fingerprint,
            "schema_version": schema_version,
            "prompt_version": prompt_version,
            "model": model,
            "assessment_json": dict(assessment),
            "status": status,
            "created_at": now,
            "updated_at": now,
        }
        dialect = self.database.engine.dialect.name
        if dialect in {"postgresql", "sqlite"}:
            insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
            statement = insert(EventSemanticCache).values(**values)
            conflict_columns = [
                EventSemanticCache.participant_id,
                EventSemanticCache.fingerprint,
                EventSemanticCache.schema_version,
                EventSemanticCache.prompt_version,
                EventSemanticCache.model,
            ]
            incoming_rank = case(
                (statement.excluded.status == "complete", 3),
                (statement.excluded.status == "partial", 2),
                else_=1,
            )
            stored_rank = case(
                (EventSemanticCache.status == "complete", 3),
                (EventSemanticCache.status == "partial", 2),
                else_=1,
            )
            statement = statement.on_conflict_do_update(
                index_elements=conflict_columns,
                set_={
                    "assessment_json": statement.excluded.assessment_json,
                    "status": statement.excluded.status,
                    "updated_at": statement.excluded.updated_at,
                },
                where=incoming_rank >= stored_rank,
            )
            with self.database.session() as session:
                session.execute(statement)
            return
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
                    model=model, assessment_json=dict(assessment), status=status,
                ))
            else:
                quality = {"rejected": 1, "partial": 2, "complete": 3}
                if quality[status] >= quality.get(str(row.status), 0):
                    row.assessment_json = dict(assessment)
                    row.status = status
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

    def valid_dates(self, participant_id: uuid.UUID) -> set[date]:
        """Return only dates that already have a current persisted Forecast."""

        with self.database.session() as session:
            rows = session.execute(
                select(ForecastSnapshot.local_date).where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.valid.is_(True),
                ).distinct()
            ).scalars().all()
            return set(rows)

    def get(
        self,
        participant_id: uuid.UUID,
        forecast_id: uuid.UUID | str,
        *,
        local_date: date | None = None,
    ) -> Optional[dict[str, Any]]:
        """Fetch one exact snapshot without requiring it to remain current."""

        try:
            snapshot_id = uuid.UUID(str(forecast_id))
        except ValueError:
            return None
        conditions = [
            ForecastSnapshot.id == snapshot_id,
            ForecastSnapshot.participant_id == participant_id,
        ]
        if local_date is not None:
            conditions.append(ForecastSnapshot.local_date == local_date)
        with self.database.session() as session:
            row = session.execute(
                select(ForecastSnapshot).where(*conditions)
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

        This is an artifact-generation query only.  Consumers that need the
        forecast which was current at the cutoff must use :meth:`current_at`.
        """

        with self.database.session() as session:
            row = session.execute(select(ForecastSnapshot).where(
                ForecastSnapshot.participant_id == participant_id,
                ForecastSnapshot.local_date == local_date,
                ForecastSnapshot.generated_at < timestamp,
            ).order_by(desc(ForecastSnapshot.generated_at)).limit(1)).scalar_one_or_none()
            return self._view(row) if row is not None else None

    def current_at(
        self, participant_id: uuid.UUID, local_date: date, timestamp: datetime
    ) -> Optional[dict[str, Any]]:
        """Return the forecast that was actually current at ``timestamp``."""

        cutoff = (
            timestamp.replace(tzinfo=timezone.utc)
            if timestamp.tzinfo is None
            else timestamp.astimezone(timezone.utc)
        )
        with self.database.session() as session:
            event = session.execute(
                select(ForecastCurrentnessEvent).where(
                    ForecastCurrentnessEvent.participant_id == participant_id,
                    ForecastCurrentnessEvent.local_date == local_date,
                    ForecastCurrentnessEvent.occurred_at <= cutoff,
                ).order_by(
                    desc(ForecastCurrentnessEvent.occurred_at),
                    desc(ForecastCurrentnessEvent.id),
                ).limit(1)
            ).scalar_one_or_none()
            if event is None or event.event_type != "activated":
                return None
            row = session.get(ForecastSnapshot, event.forecast_id)
            return self._view(row) if row is not None else None

    @staticmethod
    def _append_currentness_event(
        session: Any,
        row: ForecastSnapshot,
        *,
        event_type: str,
        reason: str,
        occurred_at: datetime,
    ) -> None:
        if event_type not in {"activated", "invalidated"}:
            raise ValueError("forecast currentness event type is invalid")
        session.add(ForecastCurrentnessEvent(
            participant_id=row.participant_id,
            local_date=row.local_date,
            forecast_id=row.id,
            forecast_version=row.forecast_version,
            event_type=event_type,
            reason=reason[:128],
            occurred_at=occurred_at,
        ))

    @staticmethod
    def _transition_time_in_session(
        session: Any,
        participant_id: uuid.UUID,
        local_date: date,
        requested: datetime,
    ) -> datetime:
        """Keep transition timestamps strictly ordered under participant lock."""

        latest = session.execute(
            select(ForecastCurrentnessEvent.occurred_at).where(
                ForecastCurrentnessEvent.participant_id == participant_id,
                ForecastCurrentnessEvent.local_date == local_date,
            ).order_by(
                desc(ForecastCurrentnessEvent.occurred_at),
                desc(ForecastCurrentnessEvent.id),
            ).limit(1)
        ).scalar_one_or_none()
        if latest is None:
            return requested
        latest_aware = (
            latest.replace(tzinfo=timezone.utc)
            if latest.tzinfo is None
            else latest.astimezone(timezone.utc)
        )
        return max(requested, latest_aware + timedelta(microseconds=1))

    def invalidate_current_for_date(
        self,
        warning_repository: "WarningScheduleRepository",
        participant_id: uuid.UUID,
        local_date: date,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Atomically invalidate active forecasts and cancel unsent derivatives."""

        if warning_repository.database is not self.database:
            raise ValueError("forecast and warning repositories must share a database")
        with self.database.session() as session:
            session.get(Participant, participant_id, with_for_update=True)
            changed_at = self._transition_time_in_session(
                session, participant_id, local_date, now or utc_now()
            )
            rows = session.execute(
                select(ForecastSnapshot).where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date == local_date,
                    ForecastSnapshot.valid.is_(True),
                ).with_for_update()
            ).scalars().all()
            forecast_ids = [row.id for row in rows]
            for row in rows:
                row.valid = False
                self._append_currentness_event(
                    session, row, event_type="invalidated", reason=reason,
                    occurred_at=changed_at,
                )
            cancelled = warning_repository._cancel_for_forecasts_in_session(
                session,
                forecast_ids,
                reason=reason,
                now=changed_at,
            )
            return {"forecasts_invalidated": len(rows), "warnings_cancelled": cancelled}

    def invalidate_for_calendar_mutation(
        self,
        warning_repository: "WarningScheduleRepository",
        participant_id: uuid.UUID,
        local_date: date,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Fence stale calculations after a successful remote calendar write."""

        return self.invalidate_for_calendar_mutation_dates(
            warning_repository,
            participant_id,
            {local_date},
            reason=reason,
            now=now,
        )

    def invalidate_for_calendar_mutation_dates(
        self,
        warning_repository: "WarningScheduleRepository",
        participant_id: uuid.UUID,
        local_dates: set[date],
        *,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Atomically fence all dates changed by one remote Calendar mutation."""

        if warning_repository.database is not self.database:
            raise ValueError("forecast and warning repositories must share a database")
        targets = sorted(set(local_dates))
        if not targets:
            return {"forecasts_invalidated": 0, "warnings_cancelled": 0}
        changed_at = now or utc_now()
        with self.database.session() as session:
            session.get(Participant, participant_id, with_for_update=True)
            calendars = session.execute(
                select(CalendarSnapshot).where(
                    CalendarSnapshot.participant_id == participant_id,
                    CalendarSnapshot.local_date.in_(targets),
                ).with_for_update()
            ).scalars().all()
            calendars_by_date = {row.local_date: row for row in calendars}
            mutation_id = uuid.uuid4().hex
            for local_date in targets:
                calendar = calendars_by_date.get(local_date)
                marker = f"mutation:{mutation_id}:{local_date.isoformat()}"
                if calendar is None:
                    session.add(
                        CalendarSnapshot(
                            participant_id=participant_id,
                            local_date=local_date,
                            calendar_revision=marker,
                            events_json=[],
                            snapshot_state=(
                                CalendarSnapshotRepository.MUTATION_REFRESH_PENDING
                            ),
                            degraded=True,
                            last_refresh_attempt_at=changed_at,
                            last_refresh_error_class=(
                                "calendar_mutation_refresh_pending"
                            ),
                            updated_at=changed_at,
                        )
                    )
                    continue
                calendar.calendar_revision = marker
                # Retain the last known events for diagnostics only.  The
                # explicit pending state prevents Forecast from consuming them.
                calendar.snapshot_state = (
                    CalendarSnapshotRepository.MUTATION_REFRESH_PENDING
                )
                calendar.degraded = True
                calendar.last_refresh_attempt_at = changed_at
                calendar.last_refresh_error_class = "calendar_mutation_refresh_pending"
                calendar.updated_at = changed_at

            rows = session.execute(
                select(ForecastSnapshot).where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date.in_(targets),
                    ForecastSnapshot.valid.is_(True),
                ).with_for_update()
            ).scalars().all()
            forecast_ids = [row.id for row in rows]
            for row in rows:
                row.valid = False
                event_time = self._transition_time_in_session(
                    session, participant_id, row.local_date, changed_at
                )
                self._append_currentness_event(
                    session, row, event_type="invalidated", reason=reason,
                    occurred_at=event_time,
                )
            cancelled = warning_repository._cancel_for_forecasts_in_session(
                session,
                forecast_ids,
                reason=reason,
                now=changed_at,
            )
            return {"forecasts_invalidated": len(rows), "warnings_cancelled": cancelled}

    def save(
        self, participant_id: uuid.UUID, local_date: date, *,
        calendar_revision: str, semantic_revision: str, algorithm_version: str,
        forecast_version: str, semantic_status: str, semantic_input: list[dict[str, Any]],
        curve: list[dict[str, Any]], peaks: list[dict[str, Any]],
        warning_windows: list[dict[str, Any]], output: dict[str, Any],
        observation_revision: str = "",
        observation_window_start: datetime | None = None,
        observation_window_end: datetime | None = None,
        verify_current_inputs: bool = False,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            return self._save_in_session(
                session, participant_id, local_date,
                calendar_revision=calendar_revision, semantic_revision=semantic_revision,
                observation_revision=observation_revision,
                algorithm_version=algorithm_version, forecast_version=forecast_version,
                semantic_status=semantic_status, semantic_input=semantic_input,
                curve=curve, peaks=peaks, warning_windows=warning_windows, output=output,
                observation_window_start=observation_window_start,
                observation_window_end=observation_window_end,
                verify_current_inputs=verify_current_inputs,
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
        observation_window_start: datetime | None = None,
        observation_window_end: datetime | None = None,
        verify_current_inputs: bool = False,
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
                observation_window_start=observation_window_start,
                observation_window_end=observation_window_end,
                verify_current_inputs=verify_current_inputs,
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
        observation_window_start: datetime | None = None,
        observation_window_end: datetime | None = None,
        verify_current_inputs: bool = False,
    ) -> dict[str, Any]:
        session.get(Participant, participant_id, with_for_update=True)
        changed_at = self._transition_time_in_session(
            session, participant_id, local_date, utc_now()
        )

        calendar = session.execute(
            select(CalendarSnapshot).where(
                CalendarSnapshot.participant_id == participant_id,
                CalendarSnapshot.local_date == local_date,
            ).with_for_update()
        ).scalar_one_or_none()
        if (
            verify_current_inputs
            and calendar is not None
            and (
                calendar.calendar_revision != calendar_revision
                or calendar.snapshot_state
                == CalendarSnapshotRepository.MUTATION_REFRESH_PENDING
            )
        ):
            raise ForecastInputChangedError("calendar")

        if observation_window_start is not None and observation_window_end is not None:
            start = ObservationRepository._aware(observation_window_start)
            end = ObservationRepository._aware(observation_window_end)
            rows = session.execute(
                select(StateObservation)
                .where(
                    StateObservation.participant_id == participant_id,
                    StateObservation.observed_at >= start,
                    StateObservation.observed_at < end,
                    StateObservation.observed_at <= utc_now(),
                )
                .order_by(
                    desc(StateObservation.observed_at),
                    desc(StateObservation.created_at),
                    desc(StateObservation.id),
                )
                .limit(100)
            ).scalars().all()
            current_revision = hashlib.sha256(
                _canonical_json(
                    [ObservationRepository._view(row) for row in rows]
                ).encode("utf-8")
            ).hexdigest()
            if current_revision != observation_revision:
                raise ForecastInputChangedError("observation")

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
                self._append_currentness_event(
                    session, row, event_type="invalidated",
                    reason="superseded_by_forecast", occurred_at=changed_at,
                )
        if existing is not None:
            was_valid = existing.valid
            existing.valid = True
            if not was_valid:
                self._append_currentness_event(
                    session, existing, event_type="activated",
                    reason="forecast_reactivated", occurred_at=changed_at,
                )
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
        self._append_currentness_event(
            session, row, event_type="activated", reason="forecast_published",
            occurred_at=changed_at,
        )
        session.flush()
        return self._view(row)


class WarningScheduleRepository:
    ACTIVE = {"pending", "claimed", "delivery_unavailable"}
    SUCCESSFUL = {"sent", "escalated"}

    def __init__(
        self,
        database: Database,
        delivery_policy: WarningDeliveryPolicyConfig,
        *,
        timezone_name: str = "Asia/Shanghai",
    ):
        self.database = database
        self.delivery_policy = delivery_policy
        self.timezone = ZoneInfo(timezone_name)
        self.same_day_late_care_policy = SameDayLateCarePolicy()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    @staticmethod
    def _mirror_care(session: Any, row: WarningSchedule) -> None:
        from app.repositories_care import CareInterventionRepository

        CareInterventionRepository.mirror_warning_in_session(session, row)

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
            "snoozed_from_intervention_id": (
                str(row.snoozed_from_intervention_id)
                if row.snoozed_from_intervention_id else None
            ),
            "episode_identity": row.episode_identity,
            "target_time": row.target_time.isoformat(), "risk_time": row.risk_time.isoformat(),
            "valid_until": row.valid_until.isoformat(), "warning_level": row.warning_level,
            "status": row.status, "payload": dict(row.payload_json),
            "attempt_count": row.attempt_count,
            "claim_token": str(row.claim_token) if row.claim_token else None,
            "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
            "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
            "lease_until": row.lease_until.isoformat() if row.lease_until else None,
            "authorized_at": row.authorized_at.isoformat() if row.authorized_at else None,
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        }

    def _hard_authorization_reason(
        self,
        preference: ParticipantCarePreference | None,
        now: datetime,
        payload: dict[str, Any] | None = None,
    ) -> str | None:
        if preference is None:
            return None
        if not bool(preference.care_enabled):
            return "care_disabled"
        if not bool(preference.warning_enabled):
            return "warning_disabled"
        care_plan = (payload or {}).get("care_plan")
        if (
            isinstance(care_plan, dict)
            and care_plan.get("intervention_type") == "schedule_adjustment"
            and not bool(preference.allow_schedule_suggestions)
        ):
            return "schedule_suggestions_disabled"
        if (
            preference.muted_until is not None
            and self._aware(preference.muted_until) > now
        ):
            return "muted"
        start = preference.quiet_hours_start
        end = preference.quiet_hours_end
        if start is not None and end is not None and start != end:
            local_clock = now.astimezone(self.timezone).time().replace(tzinfo=None)
            in_quiet_hours = (
                start <= local_clock < end
                if start < end
                else local_clock >= start or local_clock < end
            )
            if in_quiet_hours:
                return "quiet_hours"
        return None

    @staticmethod
    def _payload_with_source_provenance(
        payload: dict[str, Any],
        *,
        warning_id: uuid.UUID,
        forecast_id: uuid.UUID,
        forecast_version: str,
    ) -> dict[str, Any]:
        result = dict(payload)
        raw_provenance = result.get("care_provenance")
        provenance = (
            dict(raw_provenance) if isinstance(raw_provenance, dict) else {}
        )
        provenance.update(
            {
                "source_warning_id": str(warning_id),
                "source_forecast_id": str(forecast_id),
                "forecast_version": str(forecast_version),
            }
        )
        result["care_provenance"] = provenance
        return result

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

    def _cancel_for_forecasts_in_session(
        self,
        session: Any,
        forecast_ids: list[uuid.UUID],
        *,
        reason: str,
        now: datetime,
    ) -> int:
        if not forecast_ids:
            return 0
        changed_at = self._aware(now)
        rows = session.execute(
            select(WarningSchedule).where(
                WarningSchedule.forecast_id.in_(forecast_ids),
                WarningSchedule.status.in_(self.ACTIVE),
            ).with_for_update()
        ).scalars().all()
        for row in rows:
            row.status = "cancelled"
            row.claim_token = None
            row.claimed_at = None
            row.lease_until = None
            row.next_attempt_at = None
            row.payload_json = {
                **dict(row.payload_json),
                "cancellation_reason": str(reason)[:128],
            }
            row.updated_at = changed_at
            self._mirror_care(session, row)
        return len(rows)

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
                        latest_successful_at
                        + timedelta(minutes=self.delivery_policy.min_interval_minutes)
                        if latest_successful_at is not None else now
                    )
                    due = max(self._aware(item["target_time"]), next_allowed, now)
                    legal_new_window = (
                        schedule_changed
                        and due <= self._aware(item["valid_until"])
                        and due < self._aware(item["risk_time"])
                    )
                    if (
                        legal_new_window
                        and len(successful) < self.delivery_policy.max_daily_sends
                    ):
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
                if len(successful) >= self.delivery_policy.max_daily_sends:
                    status = "suppressed"
                    payload["suppression_reason"] = "daily_cap"
                    next_attempt_at = None
                row_id = uuid.uuid4()
                payload = self._payload_with_source_provenance(
                    payload,
                    warning_id=row_id,
                    forecast_id=forecast_id,
                    forecast_version=forecast_version,
                )
                created_row = WarningSchedule(
                    id=row_id,
                    participant_id=participant_id, local_date=local_date,
                    forecast_id=forecast_id, forecast_version=forecast_version,
                    warning_identity=identity, target_time=item["target_time"],
                    episode_identity=episode_identity,
                    risk_time=item["risk_time"], valid_until=item["valid_until"],
                    warning_level=item["warning_level"], status=status,
                    payload_json=payload, next_attempt_at=next_attempt_at,
                )
                session.add(created_row)
                session.flush()
                self._mirror_care(session, created_row)
                counts["created"] += 1
                continue
            unmatched.pop(row.id, None)
            if row.status in {"sent", "escalated", "failed", "expired", "suppressed"}:
                # Terminal rows are immutable audit records.  A later valid
                # opportunity is represented by a new row; an ordinary drift
                # or tier change never revives or rewrites the old record.
                self._mirror_care(session, row)
                counts["kept"] += 1
                continue
            desired_payload = self._payload_with_source_provenance(
                dict(item["payload"]),
                warning_id=row.id,
                forecast_id=forecast_id,
                forecast_version=forecast_version,
            )
            schedule_changed = (
                self._aware(row.target_time) != self._aware(item["target_time"])
                or self._aware(row.risk_time) != self._aware(item["risk_time"])
                or self._aware(row.valid_until) != self._aware(item["valid_until"])
            )
            changed = (
                row.forecast_version != forecast_version
                or dict(row.payload_json) != desired_payload
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
                and len(successful) < self.delivery_policy.max_daily_sends
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
            row.payload_json = desired_payload
            row.target_time = item["target_time"]
            row.risk_time = item["risk_time"]
            row.valid_until = item["valid_until"]
            row.warning_level = item["warning_level"]
            row.episode_identity = item["episode_identity"]
            # A desired, untouched cancelled row is reactivated even when the
            # timestamps are identical. Other active states retain semantics.
            row.updated_at = utc_now()
            self._mirror_care(session, row)
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
                self._mirror_care(session, row)
                counts["cancelled"] += 1
        return counts

    def _materialize_same_day_late_care_in_session(
        self, session: Any, now: datetime
    ) -> int:
        """Create a new factually current opportunity for missed proactive care."""

        local_today = now.astimezone(self.timezone).date()
        local_end = datetime.combine(
            local_today + timedelta(days=1), time.min, self.timezone
        ).astimezone(timezone.utc)
        if now >= local_end:
            return 0
        sources = session.execute(
            select(WarningSchedule).where(
                WarningSchedule.local_date == local_today,
                WarningSchedule.status.in_(("expired", "failed", "suppressed")),
                WarningSchedule.sent_at.is_(None),
                WarningSchedule.risk_time <= now,
            ).with_for_update()
        ).scalars().all()
        created = 0
        for source in sources:
            source_payload = dict(source.payload_json or {})
            if (
                source_payload.get("delivery_kind") == "same_day_late_care"
                or source_payload.get("user_requested_followup")
            ):
                continue
            if (
                source.status == "suppressed"
                and source_payload.get("suppression_reason")
                != "minimum_interval"
            ):
                continue
            forecast = session.get(ForecastSnapshot, source.forecast_id)
            if (
                forecast is None
                or not forecast.valid
                or forecast.forecast_version != source.forecast_version
            ):
                continue
            preference = session.get(
                ParticipantCarePreference, source.participant_id
            )
            if preference is not None and (
                not preference.care_enabled or not preference.warning_enabled
            ):
                continue
            successful_count = len(
                session.execute(
                    select(WarningSchedule.id).where(
                        WarningSchedule.participant_id == source.participant_id,
                        WarningSchedule.local_date == local_today,
                        WarningSchedule.status.in_(self.SUCCESSFUL),
                    )
                ).all()
            )
            participant_cap = (
                preference.max_proactive_care_per_day
                if preference is not None else None
            )
            effective_cap = min(
                self.delivery_policy.max_daily_sends,
                self.delivery_policy.max_daily_sends
                if participant_cap is None
                else max(0, int(participant_cap)),
            )
            if successful_count >= effective_cap:
                continue
            current_context = self._same_day_late_care_context(
                session,
                source,
                forecast,
                source_payload,
                now,
            )
            if current_context is None:
                continue
            identity = hashlib.sha256(
                f"{source.warning_identity}\0same_day_late_care".encode("utf-8")
            ).hexdigest()
            existing = session.execute(
                select(WarningSchedule.id).where(
                    WarningSchedule.participant_id == source.participant_id,
                    WarningSchedule.local_date == local_today,
                    WarningSchedule.warning_identity == identity,
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue
            row_id = uuid.uuid4()
            late_plan = self.same_day_late_care_policy.plan(
                source_warning_level=source.warning_level,
                source_care_plan=source_payload.get("care_plan"),
                current_context=current_context,
            )
            late_message = late_plan.message
            plan = dict(source_payload.get("care_plan") or {})
            plan.update(
                {
                    "intervention_type": late_plan.intervention_type,
                    "template_id": late_plan.template_id,
                    "reason_code": late_plan.reason_code,
                }
            )
            provenance = dict(source_payload.get("care_provenance") or {})
            provenance.update(
                {
                    "source_opportunity_id": str(source.id),
                    "proactive_risk_time": self._aware(source.risk_time).isoformat(),
                }
            )
            payload = {
                **source_payload,
                "message": late_message,
                "fallback_message": late_message,
                "delivery_kind": "same_day_late_care",
                "source_opportunity_id": str(source.id),
                "late_care_context": current_context,
                "care_plan": plan,
                "care_provenance": provenance,
            }
            payload = self._payload_with_source_provenance(
                payload,
                warning_id=row_id,
                forecast_id=source.forecast_id,
                forecast_version=source.forecast_version,
            )
            late_care = WarningSchedule(
                id=row_id,
                participant_id=source.participant_id,
                local_date=local_today,
                forecast_id=source.forecast_id,
                forecast_version=source.forecast_version,
                warning_identity=identity,
                episode_identity=hashlib.sha256(
                    f"{source.episode_identity}\0same_day_late_care".encode("utf-8")
                ).hexdigest(),
                target_time=now,
                risk_time=local_end,
                valid_until=local_end,
                warning_level=source.warning_level,
                status="pending",
                payload_json=payload,
                next_attempt_at=now,
                updated_at=now,
            )
            session.add(late_care)
            session.flush()
            self._mirror_care(session, late_care)
            created += 1
        return created

    def _same_day_late_care_context(
        self,
        session: Any,
        source: WarningSchedule,
        forecast: ForecastSnapshot,
        source_payload: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any] | None:
        """Re-evaluate current facts instead of replaying the old warning."""

        local_now = now.astimezone(self.timezone)
        reasons: list[str] = []
        context: dict[str, Any] = {
            "evaluated_at": now.isoformat(),
            "forecast_id": str(forecast.id),
            "forecast_version": forecast.forecast_version,
        }

        observation = session.execute(
            select(StateObservation).where(
                StateObservation.participant_id == source.participant_id,
                StateObservation.observed_at <= now,
                StateObservation.observed_at >= now - timedelta(hours=6),
            ).order_by(desc(StateObservation.observed_at)).limit(1)
        ).scalar_one_or_none()
        if observation is not None:
            payload = dict(observation.payload_json or {})
            stress = payload.get("stress_0_10")
            energy = payload.get("energy_0_10")
            try:
                stress_value = float(stress) if stress is not None else None
                energy_value = float(energy) if energy is not None else None
            except (TypeError, ValueError):
                stress_value = energy_value = None
            context["observation_id"] = str(observation.id)
            context["observation_stress_0_10"] = stress_value
            context["observation_energy_0_10"] = energy_value
            observation_age = now - self._aware(observation.observed_at)
            if (
                observation_age <= timedelta(minutes=90)
                and stress_value is not None
                and energy_value is not None
                and stress_value <= 3.5
                and energy_value >= 6.0
            ):
                # A fresh, explicitly good check-in is stronger evidence than
                # the older proactive forecast and suppresses late care.
                return None
            if (
                (stress_value is not None and stress_value >= 5.5)
                or (energy_value is not None and energy_value <= 4.0)
            ):
                reasons.append("recent_observation_relevant")

        calendar = session.execute(
            select(CalendarSnapshot).where(
                CalendarSnapshot.participant_id == source.participant_id,
                CalendarSnapshot.local_date == source.local_date,
                CalendarSnapshot.calendar_revision == forecast.calendar_revision,
                CalendarSnapshot.snapshot_state == "current",
            ).limit(1)
        ).scalar_one_or_none()
        relevant_event_ids: list[str] = []
        if calendar is not None and not calendar.degraded:
            for event in list(calendar.events_json or []):
                try:
                    start = datetime.fromisoformat(
                        str(event.get("start_time") or "").replace("Z", "+00:00")
                    ).astimezone(self.timezone)
                    end = datetime.fromisoformat(
                        str(event.get("end_time") or "").replace("Z", "+00:00")
                    ).astimezone(self.timezone)
                except (AttributeError, TypeError, ValueError):
                    continue
                if (
                    start - timedelta(minutes=60)
                    <= local_now
                    <= end + timedelta(minutes=60)
                ):
                    relevant_event_ids.append(
                        str(event.get("id") or event.get("event_id") or "")
                    )
            if relevant_event_ids:
                reasons.append("current_calendar_relevant")
        context["calendar_event_ids"] = relevant_event_ids

        curve = list(forecast.curve_json or [])
        minute = local_now.hour * 60 + local_now.minute
        current_point: dict[str, Any] | None = None
        for point in curve:
            try:
                hour, minute_value = (
                    int(value) for value in str(point.get("time") or "").split(":")[:2]
                )
            except (AttributeError, TypeError, ValueError):
                continue
            if hour * 60 + minute_value <= minute:
                current_point = point
            else:
                break
        if current_point is not None:
            try:
                current_stress = float(
                    current_point.get("stress_0_10")
                    if current_point.get("stress_0_10") is not None
                    else float(current_point.get("S") or 0.0) / 10.0
                )
                source_stress = float(source_payload.get("S") or 7.0) / (
                    10.0 if float(source_payload.get("S") or 7.0) > 10.0 else 1.0
                )
            except (TypeError, ValueError):
                current_stress = 0.0
                source_stress = 7.0
            context["current_predicted_stress_0_10"] = round(current_stress, 3)
            if current_stress >= max(5.5, source_stress - 2.0):
                reasons.append("current_forecast_relevant")

        if not reasons:
            return None
        context["relevance_reasons"] = sorted(set(reasons))
        return context

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
                WarningSchedule.status.in_(self.ACTIVE),
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
                self._mirror_care(session, row)
            session.flush()
            self._materialize_same_day_late_care_in_session(session, now)
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

    def claim_if_current(
        self, warning_id: uuid.UUID, *, now: datetime | None = None,
        lease_seconds: int = 120,
    ) -> Optional[dict[str, Any]]:
        now = self._aware(now or utc_now())
        with self.database.session() as session:
            candidate = session.get(WarningSchedule, warning_id)
            if candidate is None:
                return None
            # Participant is the per-user lock root. Preference updates take
            # the same lock before cancelling Warning rows, preventing a
            # preference/claim race and inconsistent lock ordering.
            participant = session.get(
                Participant,
                candidate.participant_id,
                with_for_update=True,
            )
            row = session.execute(
                select(WarningSchedule)
                .where(WarningSchedule.id == warning_id)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or row.status not in {"pending", "claimed"}:
                return None
            if row.status == "claimed" and row.lease_until and self._aware(row.lease_until) >= now:
                return None
            if self._aware(row.valid_until) < now or self._aware(row.risk_time) <= now:
                row.status = "expired"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.authorized_at = None
                row.updated_at = now
                self._mirror_care(session, row)
                return None
            participant_preference = session.get(
                ParticipantCarePreference, row.participant_id
            )
            authorization_reason = (
                "inactive"
                if participant is None or participant.status != "active"
                else "follow_up_disabled"
                if bool(row.payload_json.get("user_requested_followup"))
                and participant_preference is not None
                and not participant_preference.allow_follow_up
                else self._hard_authorization_reason(
                    participant_preference, now, dict(row.payload_json)
                )
            )
            if authorization_reason is not None:
                if (
                    authorization_reason == "quiet_hours"
                    and row.payload_json.get("delivery_kind")
                    == "same_day_late_care"
                ):
                    row.status = "pending"
                    row.claim_token = None
                    row.claimed_at = None
                    row.lease_until = None
                    row.authorized_at = None
                    retry_at = now + timedelta(minutes=5)
                    if retry_at >= self._aware(row.risk_time):
                        row.status = "expired"
                        row.next_attempt_at = None
                    else:
                        row.next_attempt_at = retry_at
                    row.updated_at = now
                    self._mirror_care(session, row)
                    return None
                row.status = "cancelled"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.authorized_at = None
                row.next_attempt_at = None
                row.payload_json = {
                    **dict(row.payload_json),
                    "cancellation_reason": f"participant_{authorization_reason}",
                }
                row.updated_at = now
                self._mirror_care(session, row)
                return None
            forecast = session.get(ForecastSnapshot, row.forecast_id)
            if forecast is None or not forecast.valid or forecast.forecast_version != row.forecast_version:
                row.status = "cancelled"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.updated_at = utc_now()
                self._mirror_care(session, row)
                return None
            # Serialize claims for this participant.  Counting only sent rows
            # is semantically correct, while a live claim acts as a temporary
            # reservation so two workers cannot send concurrently before the
            # first one records success.
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
                self._mirror_care(session, row)
                return None

            successful = session.execute(select(WarningSchedule).where(
                WarningSchedule.participant_id == row.participant_id,
                WarningSchedule.local_date == row.local_date,
                WarningSchedule.status.in_(("sent", "escalated")),
            ).order_by(desc(WarningSchedule.sent_at))).scalars().all()
            participant_max = (
                participant_preference.max_proactive_care_per_day
                if participant_preference is not None else None
            )
            effective_daily_cap = min(
                self.delivery_policy.max_daily_sends,
                (
                    self.delivery_policy.max_daily_sends
                    if participant_max is None else max(0, participant_max)
                ),
            )
            if len(successful) >= effective_daily_cap:
                row.status = "suppressed"
                row.payload_json = {**dict(row.payload_json), "suppression_reason": "daily_cap"}
                row.next_attempt_at = None
                row.updated_at = now
                self._mirror_care(session, row)
                return None
            if (
                successful
                and self.delivery_policy.min_interval_minutes > 0
                and not bool(row.payload_json.get("user_requested_followup"))
            ):
                latest = next((item for item in successful if item.sent_at is not None), None)
                if latest is not None:
                    next_allowed = self._aware(latest.sent_at) + timedelta(
                        minutes=self.delivery_policy.min_interval_minutes
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
                        self._mirror_care(session, row)
                        return None
            row.status = "claimed"
            row.claimed_at = now
            row.lease_until = now + timedelta(seconds=max(1, lease_seconds))
            row.claim_token = uuid.uuid4()
            row.authorized_at = None
            row.updated_at = now
            self._mirror_care(session, row)
            return self._view(row)

    def validate_claim_current(
        self, warning_id: uuid.UUID, *, claim_token: uuid.UUID | str,
        expected_forecast_version: str, now: datetime,
    ) -> bool:
        now = self._aware(now)
        token = uuid.UUID(str(claim_token))
        with self.database.session() as session:
            candidate = session.get(WarningSchedule, warning_id)
            if candidate is None:
                return False
            participant = session.get(
                Participant, candidate.participant_id, with_for_update=True
            )
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
                self._mirror_care(session, row)
                return False
            preference = session.get(
                ParticipantCarePreference, row.participant_id
            )
            authorization_reason = (
                "inactive"
                if participant is None or participant.status != "active"
                else "follow_up_disabled"
                if bool(row.payload_json.get("user_requested_followup"))
                and preference is not None
                and not preference.allow_follow_up
                else self._hard_authorization_reason(
                    preference, now, dict(row.payload_json)
                )
            )
            if authorization_reason is not None:
                row.status = "cancelled"
                row.claim_token = None
                row.claimed_at = None
                row.lease_until = None
                row.authorized_at = None
                row.next_attempt_at = None
                row.payload_json = {
                    **dict(row.payload_json),
                    "cancellation_reason": f"participant_{authorization_reason}",
                }
                row.updated_at = now
                self._mirror_care(session, row)
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
                self._mirror_care(session, row)
                return False
            row.authorized_at = now
            row.updated_at = now
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
            candidate = session.get(WarningSchedule, warning_id)
            if candidate is None:
                return False
            session.get(
                Participant, candidate.participant_id, with_for_update=True
            )
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if (
                row is None
                or row.status != "claimed"
                or row.claim_token != token
                or row.forecast_version != expected_forecast_version
            ):
                return False
            if sent:
                forecast = session.get(ForecastSnapshot, row.forecast_id)
                if (
                    row.authorized_at is None
                    or now < self._aware(row.authorized_at)
                    or row.claimed_at is None
                    or row.lease_until is None
                    or self._aware(row.lease_until) < now
                    or forecast is None
                    or not forecast.valid
                    or forecast.forecast_version != expected_forecast_version
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
                    row.authorized_at = None
            row.updated_at = now
            self._mirror_care(session, row)
            return True

    def block_delivery(
        self, warning_id: uuid.UUID, *, claim_token: uuid.UUID | str,
        expected_forecast_version: str, now: datetime, reason: str,
    ) -> bool:
        token = uuid.UUID(str(claim_token))
        with self.database.session() as session:
            candidate = session.get(WarningSchedule, warning_id)
            if candidate is None:
                return False
            session.get(
                Participant, candidate.participant_id, with_for_update=True
            )
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
            self._mirror_care(session, row)
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
                self._mirror_care(session, row)
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
            candidate = session.get(WarningSchedule, warning_id)
            if candidate is None:
                return
            session.get(
                Participant, candidate.participant_id, with_for_update=True
            )
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
            self._mirror_care(session, row)


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
        except IntegrityError as exc:
            if not _integrity_matches(
                exc,
                constraint_names={"bot_events_pkey"},
                sqlite_columns=("bot_events.event_id",),
            ):
                raise
            with self.database.session() as session:
                if session.get(BotEvent, event_id) is None:
                    raise
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
                logger.warning(
                    "legacy_reply_plan_recovered",
                    extra={"event_id": row.event_id},
                )
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
            "presentation_agent_attempted",
            "presentation_agent_outcome",
            "presentation_agent_latency_ms",
            "presentation_cleanup_pending",
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
