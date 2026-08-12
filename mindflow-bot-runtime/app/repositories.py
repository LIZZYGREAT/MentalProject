"""Participant-scoped repositories. Every user query takes a trusted UUID."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
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
    Participant,
    ParticipantProfile,
    ForecastSnapshot,
    PredictionRun,
    StateObservation,
    WarningSchedule,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
        with self.database.session() as session:
            if source_message_id:
                existing = session.execute(
                    select(StateObservation).where(
                        StateObservation.participant_id == participant_id,
                        StateObservation.source_message_id == source_message_id,
                        StateObservation.observation_type == observation_type,
                    )
                ).scalar_one_or_none()
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

    def recent(self, participant_id: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(StateObservation)
                .where(StateObservation.participant_id == participant_id)
                .order_by(desc(StateObservation.observed_at))
                .limit(max(1, min(int(limit), 100)))
            ).scalars()
            return [
                {
                    "id": str(row.id),
                    "type": row.observation_type,
                    "payload": dict(row.payload_json),
                    "observed_at": row.observed_at.isoformat(),
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
    ) -> tuple[dict[str, Any], bool]:
        with self.database.session() as session:
            row = session.execute(
                select(CalendarSnapshot).where(
                    CalendarSnapshot.participant_id == participant_id,
                    CalendarSnapshot.local_date == local_date,
                ).with_for_update()
            ).scalar_one_or_none()
            changed = row is None or row.calendar_revision != revision
            if row is None:
                row = CalendarSnapshot(
                    participant_id=participant_id, local_date=local_date,
                    calendar_revision=revision, events_json=list(events), degraded=degraded,
                )
                session.add(row)
            elif changed or row.degraded != degraded:
                row.calendar_revision = revision
                row.events_json = list(events)
                row.degraded = degraded
                row.updated_at = utc_now()
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

    def save(
        self, participant_id: uuid.UUID, local_date: date, *,
        calendar_revision: str, semantic_revision: str, algorithm_version: str,
        forecast_version: str, semantic_status: str, semantic_input: list[dict[str, Any]],
        curve: list[dict[str, Any]], peaks: list[dict[str, Any]],
        warning_windows: list[dict[str, Any]], output: dict[str, Any],
    ) -> dict[str, Any]:
        with self.database.session() as session:
            existing = session.execute(
                select(ForecastSnapshot).where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date == local_date,
                    ForecastSnapshot.forecast_version == forecast_version,
                )
            ).scalar_one_or_none()
            if existing:
                return self._view(existing)
            stale = session.execute(
                select(ForecastSnapshot).where(
                    ForecastSnapshot.participant_id == participant_id,
                    ForecastSnapshot.local_date == local_date,
                    ForecastSnapshot.valid.is_(True),
                )
            ).scalars().all()
            for row in stale:
                row.valid = False
            row = ForecastSnapshot(
                participant_id=participant_id, local_date=local_date,
                calendar_revision=calendar_revision, semantic_revision=semantic_revision,
                algorithm_version=algorithm_version, forecast_version=forecast_version,
                semantic_status=semantic_status, semantic_input_json=list(semantic_input),
                curve_json=list(curve), peaks_json=list(peaks),
                warning_windows_json=list(warning_windows), output_json=dict(output), valid=True,
            )
            session.add(row)
            session.flush()
            return self._view(row)


class WarningScheduleRepository:
    ACTIVE = {"pending", "claimed"}

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    @staticmethod
    def _view(row: WarningSchedule) -> dict[str, Any]:
        return {
            "id": str(row.id), "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(), "forecast_id": str(row.forecast_id),
            "forecast_version": row.forecast_version, "warning_identity": row.warning_identity,
            "target_time": row.target_time.isoformat(), "warning_level": row.warning_level,
            "status": row.status, "payload": dict(row.payload_json),
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        }

    def sync(
        self, participant_id: uuid.UUID, local_date: date, *, forecast_id: uuid.UUID,
        forecast_version: str, warnings: list[dict[str, Any]], now: datetime,
    ) -> dict[str, int]:
        counts = {"kept": 0, "created": 0, "rescheduled": 0, "cancelled": 0}
        now = self._aware(now)
        desired = {
            item["warning_identity"]: item for item in warnings
            if self._aware(item["target_time"]) > now
        }
        with self.database.session() as session:
            rows = session.execute(
                select(WarningSchedule).where(
                    WarningSchedule.participant_id == participant_id,
                    WarningSchedule.local_date == local_date,
                ).with_for_update()
            ).scalars().all()
            existing = {row.warning_identity: row for row in rows}
            for identity, row in existing.items():
                item = desired.pop(identity, None)
                if item is None:
                    if row.status in self.ACTIVE and self._aware(row.target_time) > now:
                        row.status = "cancelled"
                        row.updated_at = utc_now()
                        counts["cancelled"] += 1
                    continue
                if row.status == "sent":
                    counts["kept"] += 1
                    continue
                changed = row.target_time != item["target_time"] or row.warning_level != item["warning_level"]
                row.forecast_id = forecast_id
                row.forecast_version = forecast_version
                row.payload_json = dict(item["payload"])
                row.target_time = item["target_time"]
                row.warning_level = item["warning_level"]
                row.status = "pending"
                row.updated_at = utc_now()
                counts["rescheduled" if changed else "kept"] += 1
            for identity, item in desired.items():
                session.add(WarningSchedule(
                    participant_id=participant_id, local_date=local_date,
                    forecast_id=forecast_id, forecast_version=forecast_version,
                    warning_identity=identity, target_time=item["target_time"],
                    warning_level=item["warning_level"], status="pending",
                    payload_json=dict(item["payload"]),
                ))
                counts["created"] += 1
        return counts

    def pending(self, now: datetime, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.execute(
                select(WarningSchedule).join(
                    ForecastSnapshot, ForecastSnapshot.id == WarningSchedule.forecast_id
                ).where(
                    WarningSchedule.status == "pending",
                    WarningSchedule.target_time <= now,
                    ForecastSnapshot.valid.is_(True),
                    ForecastSnapshot.forecast_version == WarningSchedule.forecast_version,
                ).order_by(WarningSchedule.target_time).limit(limit)
            ).scalars().all()
            return [self._view(row) for row in rows]

    def mark_sent_if_current(self, warning_id: uuid.UUID, now: datetime) -> bool:
        with self.database.session() as session:
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if row is None or row.status != "pending":
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

    def claim_if_current(self, warning_id: uuid.UUID) -> Optional[dict[str, Any]]:
        with self.database.session() as session:
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if row is None or row.status != "pending":
                return None
            forecast = session.get(ForecastSnapshot, row.forecast_id)
            if forecast is None or not forecast.valid or forecast.forecast_version != row.forecast_version:
                row.status = "cancelled"
                row.updated_at = utc_now()
                return None
            row.status = "claimed"
            row.updated_at = utc_now()
            return self._view(row)

    def finish_claim(self, warning_id: uuid.UUID, *, sent: bool, now: datetime) -> None:
        with self.database.session() as session:
            row = session.get(WarningSchedule, warning_id, with_for_update=True)
            if row is None or row.status != "claimed":
                return
            row.status = "sent" if sent else "pending"
            row.sent_at = now if sent else None
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

    def stage_reply(self, event_id: str, text: str) -> None:
        with self.database.session() as session:
            row = session.get(BotEvent, event_id, with_for_update=True)
            if row is None:
                return
            row.reply_text = str(text)[:4000]
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
