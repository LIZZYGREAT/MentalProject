"""Opt-in proof that the real 0016 -> 0017 PostgreSQL migration is executable."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from app.db import build_engine


RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def test_real_postgres_upgrade_0016_to_0017_preserves_and_backfills():
    raw_url = os.environ.get("MINDFLOW_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not configured")
    parsed = make_url(raw_url)
    if not parsed.drivername.startswith("postgresql"):
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not PostgreSQL")
    if "test" not in str(parsed.database or "").casefold():
        pytest.fail("refusing migration test outside a test database")

    schema = f"mindflow_migration_{uuid.uuid4().hex}"
    participant_id = uuid.uuid4()
    calendar_id = uuid.uuid4()
    forecast_id = uuid.uuid4()
    source_warning_id = uuid.uuid4()
    child_warning_id = uuid.uuid4()
    invalid_warning_id = uuid.uuid4()
    now = datetime(2030, 1, 15, 2, 0, tzinfo=timezone.utc)
    engine = build_engine(raw_url)
    config = Config(str(RUNTIME_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(RUNTIME_ROOT / "migrations"))

    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config.attributes["connection"] = connection
            command.upgrade(config, "0016_care_intervention_feedback")

            connection.execute(
                text(
                    "INSERT INTO participants (id, participant_code) "
                    "VALUES (:id, 'MIGRATION-TEST')"
                ),
                {"id": participant_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO calendar_snapshots (
                        id, participant_id, local_date, calendar_revision,
                        events_json, degraded
                    ) VALUES (
                        :id, :participant_id, :local_date, 'calendar-v1',
                        CAST(:events AS JSONB), false
                    )
                    """
                ),
                {
                    "id": calendar_id,
                    "participant_id": participant_id,
                    "local_date": date(2030, 1, 15),
                    "events": json.dumps([{"id": "event-1"}]),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO forecast_snapshots (
                        id, participant_id, local_date, calendar_revision,
                        semantic_revision, algorithm_version, forecast_version,
                        semantic_status, semantic_input_json, curve_json,
                        peaks_json, warning_windows_json, output_json
                    ) VALUES (
                        :id, :participant_id, :local_date, 'calendar-v1',
                        'semantic-v1', 'algorithm-v1', 'forecast-v1',
                        'rules_only', '[]'::jsonb, '[]'::jsonb,
                        '[]'::jsonb, '[]'::jsonb, '{}'::jsonb
                    )
                    """
                ),
                {
                    "id": forecast_id,
                    "participant_id": participant_id,
                    "local_date": date(2030, 1, 15),
                },
            )
            warning_sql = text(
                """
                INSERT INTO warning_schedules (
                    id, participant_id, local_date, forecast_id,
                    forecast_version, warning_identity, episode_identity,
                    target_time, risk_time, valid_until, warning_level,
                    status, payload_json
                ) VALUES (
                    :id, :participant_id, :local_date, :forecast_id,
                    'forecast-v1', :identity, :identity,
                    :target_time, :risk_time, :valid_until, '2',
                    :status, CAST(:payload AS JSONB)
                )
                """
            )
            connection.execute(
                warning_sql,
                {
                    "id": source_warning_id,
                    "participant_id": participant_id,
                    "local_date": date(2030, 1, 15),
                    "forecast_id": forecast_id,
                    "identity": "source-warning",
                    "target_time": now,
                    "risk_time": now + timedelta(minutes=20),
                    "valid_until": now + timedelta(minutes=10),
                    "status": "sent",
                    "payload": "{}",
                },
            )
            connection.execute(
                warning_sql,
                {
                    "id": invalid_warning_id,
                    "participant_id": participant_id,
                    "local_date": date(2030, 1, 15),
                    "forecast_id": forecast_id,
                    "identity": "invalid-provenance-warning",
                    "target_time": now + timedelta(minutes=31),
                    "risk_time": now + timedelta(minutes=46),
                    "valid_until": now + timedelta(minutes=41),
                    "status": "pending",
                    "payload": json.dumps(
                        {"snoozed_from_intervention_id": "not-a-uuid"}
                    ),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO care_intervention_events (
                        id, participant_id, source_warning_id,
                        source_forecast_id, forecast_version,
                        intervention_type, template_id, template_version,
                        reason_code, scheduled_at, status, delivery_status,
                        message_text, context_json, actions_json,
                        created_at, updated_at
                    ) VALUES (
                        :id, :participant_id, :id, :forecast_id,
                        'forecast-v1', 'protected_break', 'protected-break-v1',
                        '1.0.0', 'migration_test', :scheduled_at,
                        'sent', 'sent', 'message', '{}'::jsonb, '[]'::jsonb,
                        :scheduled_at, :scheduled_at
                    )
                    """
                ),
                {
                    "id": source_warning_id,
                    "participant_id": participant_id,
                    "forecast_id": forecast_id,
                    "scheduled_at": now,
                },
            )
            connection.execute(
                warning_sql,
                {
                    "id": child_warning_id,
                    "participant_id": participant_id,
                    "local_date": date(2030, 1, 15),
                    "forecast_id": forecast_id,
                    "identity": "child-warning",
                    "target_time": now + timedelta(minutes=30),
                    "risk_time": now + timedelta(minutes=45),
                    "valid_until": now + timedelta(minutes=40),
                    "status": "pending",
                    "payload": json.dumps(
                        {"snoozed_from_intervention_id": str(source_warning_id)}
                    ),
                },
            )

            command.upgrade(config, "0017_care_delivery_authorization")

            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0017_care_delivery_authorization"
            assert connection.scalar(
                text(
                    "SELECT snapshot_state FROM calendar_snapshots "
                    "WHERE id = :id"
                ),
                {"id": calendar_id},
            ) == "current"
            assert connection.scalar(
                text(
                    "SELECT snoozed_from_intervention_id FROM warning_schedules "
                    "WHERE id = :id"
                ),
                {"id": child_warning_id},
            ) == source_warning_id
            assert connection.scalar(
                text(
                    "SELECT snoozed_from_intervention_id FROM warning_schedules "
                    "WHERE id = :id"
                ),
                {"id": invalid_warning_id},
            ) is None
            assert connection.scalar(
                text("SELECT count(*) FROM forecast_snapshots WHERE id = :id"),
                {"id": forecast_id},
            ) == 1

            inspector = inspect(connection)
            warning_columns = {
                column["name"] for column in inspector.get_columns("warning_schedules")
            }
            assert {"authorized_at", "snoozed_from_intervention_id"} <= warning_columns
            unique_columns = {
                tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints("warning_schedules")
            }
            assert ("snoozed_from_intervention_id",) in unique_columns
            foreign_keys = inspector.get_foreign_keys("warning_schedules")
            assert any(
                key["constrained_columns"] == ["snoozed_from_intervention_id"]
                and key["referred_table"] == "care_intervention_events"
                for key in foreign_keys
            )
    finally:
        config.attributes.pop("connection", None)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
