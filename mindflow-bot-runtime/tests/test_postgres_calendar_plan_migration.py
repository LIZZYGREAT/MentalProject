"""Opt-in PostgreSQL regression coverage for the 0047 -> 0048 data boundary."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import text
import pytest

from app.db import build_engine
from app.postgres_test_guard import (
    get_test_postgres_connect_timeout_seconds,
    optional_test_postgres_url,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def _config(connection) -> Config:
    config = Config(str(RUNTIME_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(RUNTIME_ROOT / "migrations"))
    config.attributes["connection"] = connection
    return config


def _upgrade_0047(connection) -> None:
    command.upgrade(_config(connection), "0047_calendar_mutation_plans")


def _participant_and_plan(connection, *, status: str) -> uuid.UUID:
    participant_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    connection.execute(
        text(
            "INSERT INTO participants (id, participant_code) "
            "VALUES (:id, :code)"
        ),
        {"id": participant_id, "code": f"MIGRATION-{uuid.uuid4().hex[:8]}"},
    )
    connection.execute(
        text(
            "INSERT INTO calendar_mutation_plans "
            "(id, participant_id, operation, items_json, status, "
            "expires_at, created_at, updated_at) VALUES "
            "(:id, :participant_id, 'create', CAST(:items AS JSONB), :status, "
            ":expires_at, :created_at, :updated_at)"
        ),
        {
            "id": plan_id,
            "participant_id": participant_id,
            "items": json.dumps(
                [
                    {
                        "summary": "legacy event",
                        "start_time": "2030-01-02T08:00:00+08:00",
                        "end_time": "2030-01-02T09:00:00+08:00",
                    }
                ]
            ),
            "status": status,
            "expires_at": now,
            "created_at": now,
            "updated_at": now,
        },
    )
    return plan_id


def test_real_postgres_0047_awaiting_plan_expires_on_0048():
    raw_url = optional_test_postgres_url()
    if raw_url is None:
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not configured")
    schema = f"mindflow_plan_migration_{uuid.uuid4().hex}"
    engine = build_engine(
        raw_url,
        connect_timeout_seconds=get_test_postgres_connect_timeout_seconds(),
    )
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            _upgrade_0047(connection)
            plan_id = _participant_and_plan(
                connection, status="awaiting_confirmation"
            )
            command.upgrade(_config(connection), "0048_calendar_mutation_plan_items")
            row = connection.execute(
                text(
                    "SELECT status, result_json->>'upgrade_reason' AS reason "
                    "FROM calendar_mutation_plans WHERE id = :id"
                ),
                {"id": plan_id},
            ).one()
            assert row.status == "expired"
            assert row.reason == "legacy_plan_expired_on_0048_upgrade"
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_real_postgres_0047_processing_plan_fails_closed_before_0048():
    raw_url = optional_test_postgres_url()
    if raw_url is None:
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not configured")
    schema = f"mindflow_plan_migration_{uuid.uuid4().hex}"
    engine = build_engine(
        raw_url,
        connect_timeout_seconds=get_test_postgres_connect_timeout_seconds(),
    )
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            _upgrade_0047(connection)
            _participant_and_plan(connection, status="processing")
            with pytest.raises(RuntimeError, match="legacy processing"):
                command.upgrade(
                    _config(connection), "0048_calendar_mutation_plan_items"
                )
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0047_calendar_mutation_plans"
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_real_postgres_0047_terminal_plan_upgrades_to_0048():
    raw_url = optional_test_postgres_url()
    if raw_url is None:
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not configured")
    schema = f"mindflow_plan_migration_{uuid.uuid4().hex}"
    engine = build_engine(
        raw_url,
        connect_timeout_seconds=get_test_postgres_connect_timeout_seconds(),
    )
    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            _upgrade_0047(connection)
            _participant_and_plan(connection, status="succeeded")
            command.upgrade(_config(connection), "0048_calendar_mutation_plan_items")
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0048_calendar_mutation_plan_items"
            assert connection.execute(
                text(
                    "SELECT to_regclass('calendar_mutation_plan_items')"
                )
            ).scalar_one() == "calendar_mutation_plan_items"
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
