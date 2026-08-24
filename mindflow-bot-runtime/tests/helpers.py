from pathlib import Path

from app.db import Database, build_engine
from app.contracts.warning import WarningDeliveryPolicyConfig
from app.repositories import ParticipantRepository


def memory_database() -> Database:
    database = Database(build_engine("sqlite:///:memory:"))
    database.create_schema_for_tests()
    return database


def participant(database: Database, code: str):
    repository = ParticipantRepository(database)
    created = repository.create(code)
    return repository.set_external_llm_consent(created.id, allowed=True)


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
