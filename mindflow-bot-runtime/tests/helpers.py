from pathlib import Path
from datetime import date, datetime, timezone
from typing import Any
import uuid

from app.db import Database, build_engine
from app.contracts.warning import WarningDeliveryPolicyConfig
from app.repositories import ParticipantRepository
from app.repositories_consent import ParticipantConsentRepository
from app.services.consent_service import (
    EXTERNAL_LLM_CONSENT_TYPE,
    EXTERNAL_LLM_CONSENT_VERSION,
)
from app.models import CalendarSnapshot


def memory_database() -> Database:
    database = Database(build_engine("sqlite:///:memory:"))
    database.create_schema_for_tests()
    return database


def participant(database: Database, code: str):
    """Create a participant whose external LLM processing is user-consented."""

    repository = ParticipantRepository(database)
    created = repository.create(code)
    ParticipantConsentRepository(database).grant(
        created.id,
        EXTERNAL_LLM_CONSENT_TYPE,
        EXTERNAL_LLM_CONSENT_VERSION,
    )
    return created


def seed_legacy_external_llm_consent(database: Database, participant_id) -> None:
    """Seed the legacy researcher/CLI flag directly for audit-path tests.

    Legacy data has no production write entry anymore: the flag must never
    authorize external LLM processing, so tests construct it at the DB layer.
    """

    from datetime import datetime, timezone

    from app.models import Participant

    with database.session() as session:
        row = session.get(Participant, participant_id)
        if row is not None:
            row.external_llm_consent_at = datetime.now(timezone.utc)


def warning_repository(
    database: Database,
    *,
    max_daily_sends: int = 2,
    min_interval_minutes: int = 240,
):
    from app.repositories import WarningScheduleRepository

    return WarningScheduleRepository(
        database,
        WarningDeliveryPolicyConfig(
            max_daily_sends=max_daily_sends,
            min_interval_minutes=min_interval_minutes,
        ),
    )


def seed_calendar_snapshot(
    database: Database,
    participant_id: uuid.UUID,
    local_date: date,
    *,
    revision: str,
    events: list[dict[str, Any]],
    degraded: bool = False,
    snapshot_state: str | None = None,
) -> None:
    """Insert an explicit CalendarSnapshot fixture without exposing runtime writes."""

    now = datetime.now(timezone.utc)
    with database.session() as session:
        session.add(
            CalendarSnapshot(
                participant_id=participant_id,
                local_date=local_date,
                calendar_revision=revision,
                events_json=list(events),
                degraded=degraded,
                snapshot_state=(
                    snapshot_state
                    or ("provider_degraded" if degraded else "current")
                ),
                last_refresh_attempt_at=now,
                last_refresh_success_at=None if degraded else now,
            )
        )


def skill_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "claude-runtime"
        / "plugins"
        / "mindflow-care"
        / "skills"
        / "mental-health-care"
        / "SKILL.md"
    )
