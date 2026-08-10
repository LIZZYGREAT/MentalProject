from pathlib import Path

from app.db import Database, build_engine
from app.repositories import ParticipantRepository


def memory_database() -> Database:
    database = Database(build_engine("sqlite:///:memory:"))
    database.create_schema_for_tests()
    return database


def participant(database: Database, code: str):
    repository = ParticipantRepository(database)
    created = repository.create(code)
    return repository.set_external_llm_consent(created.id, allowed=True)


def skill_path() -> Path:
    return Path(__file__).resolve().parents[1] / "skills" / "mental-health-care" / "SKILL.md"
