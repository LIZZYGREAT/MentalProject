"""Opt-in proof that the real 0016 -> 0023 PostgreSQL migrations are executable."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import build_engine
from postgres_test_guard import optional_test_postgres_url


RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def test_real_postgres_upgrade_0016_to_0023_preserves_and_backfills():
    try:
        raw_url = optional_test_postgres_url()
    except ValueError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not configured")

    schema = f"mindflow_migration_{uuid.uuid4().hex}"
    participant_id = uuid.uuid4()
    legacy_profile_id = uuid.uuid4()
    calendar_id = uuid.uuid4()
    degraded_calendar_id = uuid.uuid4()
    forecast_id = uuid.uuid4()
    source_warning_id = uuid.uuid4()
    child_warning_id = uuid.uuid4()
    invalid_warning_id = uuid.uuid4()
    existing_review_id = uuid.uuid4()
    optional_review_id = uuid.uuid4()
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
                    INSERT INTO learned_model_profiles (
                        id, participant_id, version, parameters_json, source,
                        sample_count, day_count, confidence, window_start,
                        window_end, created_at
                    ) VALUES (
                        :id, :participant_id, 1,
                        '{"S_star_init": 47.5}'::jsonb, 'pilot-calibration-v1',
                        14, 7, 0.6, '2030-01-01', '2030-01-07', :created_at
                    )
                    """
                ),
                {
                    "id": legacy_profile_id,
                    "participant_id": participant_id,
                    "created_at": now,
                },
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
                    INSERT INTO daily_review_responses (
                        id, participant_id, local_date, revision,
                        card_version, callback_event_id, submitted_at,
                        start_stress, start_energy, peak_stress, peak_period,
                        end_stress, end_energy, energy_consumption,
                        raw_json, created_at
                    ) VALUES (
                        :id, :participant_id, :local_date, 1,
                        'daily-review-v1', 'migration-existing-review',
                        :submitted_at, 3, 8, 9, 'evening', 5, 4, 7,
                        '{}'::jsonb, :submitted_at
                    )
                    """
                ),
                {
                    "id": existing_review_id,
                    "participant_id": participant_id,
                    "local_date": date(2030, 1, 15),
                    "submitted_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO calendar_snapshots (
                        id, participant_id, local_date, calendar_revision,
                        events_json, degraded
                    ) VALUES (
                        :id, :participant_id, :local_date, 'calendar-degraded',
                        '[]'::jsonb, true
                    )
                    """
                ),
                {
                    "id": degraded_calendar_id,
                    "participant_id": participant_id,
                    "local_date": date(2030, 1, 16),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO forecast_snapshots (
                            id, participant_id, local_date, calendar_revision,
                            semantic_revision, observation_revision,
                            algorithm_version, forecast_version,
                        semantic_status, semantic_input_json, curve_json,
                        peaks_json, warning_windows_json, output_json
                    ) VALUES (
                        :id, :participant_id, :local_date, 'calendar-v1',
                            'semantic-v1', '', 'algorithm-v1', 'forecast-v1',
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
                    "SELECT snapshot_state FROM calendar_snapshots "
                    "WHERE id = :id"
                ),
                {"id": degraded_calendar_id},
            ) == "provider_degraded"
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

            command.upgrade(config, "0018_calendar_mutation_reconciliation")

            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0018_calendar_mutation_reconciliation"
            inspector = inspect(connection)
            assert "calendar_mutation_reconciliations" in inspector.get_table_names()
            reconciliation_column_rows = inspector.get_columns(
                "calendar_mutation_reconciliations"
            )
            reconciliation_columns = {
                column["name"] for column in reconciliation_column_rows
            }
            assert {
                "id",
                "participant_id",
                "mutation_kind",
                "work_json",
                "status",
                "attempt_count",
                "next_attempt_at",
                "last_error_class",
                "created_at",
                "updated_at",
                "resolved_at",
            } <= reconciliation_columns
            work_column = next(
                column
                for column in reconciliation_column_rows
                if column["name"] == "work_json"
            )
            assert str(work_column["type"]).upper() == "JSONB"
            assert any(
                key["constrained_columns"] == ["participant_id"]
                and key["referred_table"] == "participants"
                for key in inspector.get_foreign_keys(
                    "calendar_mutation_reconciliations"
                )
            )
            assert {
                "ix_calendar_mutation_reconciliation_due",
                "ix_calendar_mutation_reconciliation_participant",
            } <= {
                index["name"]
                for index in inspector.get_indexes(
                    "calendar_mutation_reconciliations"
                )
            }

            reconciliation_id = uuid.uuid4()
            work = {
                "operation": {"operation_type": "update", "event_id": "event-1"},
                "targets": [
                    {
                        "local_date": "2030-01-15",
                        "refresh_calendar": True,
                        "requires_invalidation": True,
                        "dependency_source": None,
                    }
                ],
            }
            connection.execute(
                text(
                    """
                    INSERT INTO calendar_mutation_reconciliations (
                        id, participant_id, mutation_kind, work_json,
                        status, attempt_count, next_attempt_at
                    ) VALUES (
                        :id, :participant_id, 'calendar_update_event',
                        CAST(:work AS JSONB), 'prepared', 0, :next_attempt_at
                    )
                    """
                ),
                {
                    "id": reconciliation_id,
                    "participant_id": participant_id,
                    "work": json.dumps(work),
                    "next_attempt_at": now,
                },
            )
            assert connection.scalar(
                text(
                    "SELECT work_json FROM calendar_mutation_reconciliations "
                    "WHERE id = :id"
                ),
                {"id": reconciliation_id},
            ) == work
            connection.execute(
                text(
                    "UPDATE calendar_mutation_reconciliations "
                    "SET status = 'remote_committed', attempt_count = 1 "
                    "WHERE id = :id"
                ),
                {"id": reconciliation_id},
            )
            assert connection.execute(
                text(
                    "SELECT status, attempt_count "
                    "FROM calendar_mutation_reconciliations WHERE id = :id"
                ),
                {"id": reconciliation_id},
            ).one() == ("remote_committed", 1)

            command.upgrade(config, "0024_research_evaluation")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0024_research_evaluation"
            inspector = inspect(connection)
            assert "forecast_currentness_events" in inspector.get_table_names()
            assert {
                "forecast_observation_matches",
                "dataset_snapshots",
                "model_evaluation_runs",
            } <= set(inspector.get_table_names())
            currentness_columns = {
                column["name"]
                for column in inspector.get_columns("forecast_currentness_events")
            }
            assert {
                "id", "participant_id", "local_date", "forecast_id",
                "forecast_version", "event_type", "reason", "occurred_at",
                "created_at",
            } <= currentness_columns
            assert connection.scalar(text(
                "SELECT count(*) FROM forecast_currentness_events "
                "WHERE forecast_id = :forecast_id AND event_type = 'activated'"
            ), {"forecast_id": forecast_id}) == 1
            token_columns = {
                column["name"]
                for column in inspector.get_columns("feishu_oauth_tokens")
            }
            assert {
                "refresh_lease_token", "refresh_lease_until", "refresh_started_at"
            } <= token_columns
            review_columns = {
                column["name"]: column
                for column in inspector.get_columns("daily_review_responses")
            }
            assert review_columns["energy_consumption"]["nullable"] is True
            assert {
                "psychometric_assessments",
                "participant_slow_states",
                "event_appraisal_feedback",
            } <= set(inspector.get_table_names())
            learned_columns = {
                column["name"]
                for column in inspector.get_columns("learned_model_profiles")
            }
            assert {
                "uncertainty_json",
                "model_version",
                "validation_status",
            } <= learned_columns
            legacy_row = connection.execute(
                text(
                    "SELECT parameters_json, model_version, validation_status "
                    "FROM learned_model_profiles WHERE id = :id"
                ),
                {"id": legacy_profile_id},
            ).one()
            assert legacy_row == (
                {"S_star_init": 47.5},
                "legacy",
                "candidate",
            )

            psychometric_ids = [uuid.uuid4(), uuid.uuid4()]
            for index, assessment_id in enumerate(psychometric_ids):
                connection.execute(
                    text(
                        """
                        INSERT INTO psychometric_assessments (
                            id, participant_id, instrument_name,
                            instrument_version, language, raw_items_json,
                            scores_json, administered_at, reference_period,
                            created_at
                        ) VALUES (
                            :id, :participant_id, 'PSS', '10-item-v1',
                            'zh-CN', CAST(:raw_items AS jsonb),
                            CAST(:scores AS jsonb),
                            :administered_at, 'past_month', :created_at
                        )
                        """
                    ),
                    {
                        "id": assessment_id,
                        "participant_id": participant_id,
                        "raw_items": json.dumps({"q1": index + 1}),
                        "scores": json.dumps({"total": 18 + index}),
                        "administered_at": now + timedelta(days=index),
                        "created_at": now + timedelta(days=index),
                    },
                )
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM psychometric_assessments "
                    "WHERE participant_id = :participant_id"
                ),
                {"participant_id": participant_id},
            ) == 2

            appraisal_values = {
                "id": uuid.uuid4(),
                "participant_id": participant_id,
                "event_id": "migration-event",
                "submitted_at": now,
                "created_at": now,
                "mental_demand": 8,
                "physical_demand": 2,
                "temporal_demand": 7,
                "effort": 8,
                "frustration": 6,
                "perceived_control": 4,
                "actual_stress": 7,
                "perceived_performance": 6,
            }
            appraisal_insert = text(
                """
                INSERT INTO event_appraisal_feedback (
                    id, participant_id, event_id, mental_demand,
                    physical_demand, temporal_demand, effort, frustration,
                    perceived_control, actual_stress, perceived_performance,
                    submitted_at, created_at
                ) VALUES (
                    :id, :participant_id, :event_id, :mental_demand,
                    :physical_demand, :temporal_demand, :effort, :frustration,
                    :perceived_control, :actual_stress,
                    :perceived_performance, :submitted_at, :created_at
                )
                """
            )
            connection.execute(appraisal_insert, appraisal_values)
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        appraisal_insert,
                        {
                            **appraisal_values,
                            "id": uuid.uuid4(),
                            "event_id": "invalid-score",
                            "actual_stress": 11,
                        },
                    )

            slow_state_insert = text(
                """
                INSERT INTO participant_slow_states (
                    id, participant_id, effective_at, cadence,
                    rolling_7d_stress, rolling_7d_workload,
                    rolling_7d_energy, recent_recovery_quality,
                    recent_sleep_debt, exam_period_flag, source, created_at
                ) VALUES (
                    :id, :participant_id, :effective_at, :cadence,
                    :rolling_7d_stress, 7, 5, 6, 3, false,
                    'migration-test', :created_at
                )
                """
            )
            slow_values = {
                "id": uuid.uuid4(),
                "participant_id": participant_id,
                "effective_at": now,
                "cadence": "daily",
                "rolling_7d_stress": 6,
                "created_at": now,
            }
            connection.execute(slow_state_insert, slow_values)
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        slow_state_insert,
                        {
                            **slow_values,
                            "id": uuid.uuid4(),
                            "cadence": "monthly",
                            "rolling_7d_stress": 11,
                        },
                    )

            learned_insert = text(
                """
                INSERT INTO learned_model_profiles (
                    id, participant_id, version, parameters_json,
                    uncertainty_json, source, model_version,
                    validation_status, sample_count, day_count, confidence,
                    window_start, window_end, created_at
                ) VALUES (
                    :id, :participant_id, :version,
                    '{"stress_reactivity": 1.1}'::jsonb,
                    '{"stress_reactivity": {"std_error": 0.2}}'::jsonb,
                    'migration-test', 'cal-v2', :validation_status,
                    20, 10, 0.8, :window_start, :window_end, :created_at
                )
                """
            )
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        learned_insert,
                        {
                            "id": uuid.uuid4(),
                            "participant_id": participant_id,
                            "version": 2,
                            "validation_status": "active",
                            "window_start": date(2030, 1, 1),
                            "window_end": date(2030, 1, 14),
                            "created_at": now,
                        },
                    )
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        learned_insert,
                        {
                            "id": uuid.uuid4(),
                            "participant_id": participant_id,
                            "version": 2,
                            "validation_status": "candidate",
                            "window_start": date(2030, 2, 1),
                            "window_end": date(2030, 1, 1),
                            "created_at": now,
                        },
                    )

            cascade_participant_id = uuid.uuid4()
            connection.execute(
                text(
                    "INSERT INTO participants (id, participant_code) "
                    "VALUES (:id, 'CASCADE-RESEARCH')"
                ),
                {"id": cascade_participant_id},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO psychometric_assessments (
                        id, participant_id, instrument_name,
                        instrument_version, language, raw_items_json,
                        scores_json, administered_at, created_at
                    ) VALUES (
                        :id, :participant_id, 'BRS', '6-item-v1', 'zh-CN',
                        '{}'::jsonb, '{"total": 3.5}'::jsonb, :at, :at
                    )
                    """
                ),
                {"id": uuid.uuid4(), "participant_id": cascade_participant_id, "at": now},
            )
            connection.execute(
                appraisal_insert,
                {
                    **appraisal_values,
                    "id": uuid.uuid4(),
                    "participant_id": cascade_participant_id,
                    "event_id": "cascade-event",
                },
            )
            connection.execute(
                slow_state_insert,
                {
                    **slow_values,
                    "id": uuid.uuid4(),
                    "participant_id": cascade_participant_id,
                },
            )
            connection.execute(
                learned_insert,
                {
                    "id": uuid.uuid4(),
                    "participant_id": cascade_participant_id,
                    "version": 1,
                    "validation_status": "candidate",
                    "window_start": date(2030, 1, 1),
                    "window_end": date(2030, 1, 14),
                    "created_at": now,
                },
            )
            connection.execute(
                text("DELETE FROM participants WHERE id = :id"),
                {"id": cascade_participant_id},
            )
            for table in (
                "psychometric_assessments",
                "event_appraisal_feedback",
                "participant_slow_states",
                "learned_model_profiles",
            ):
                assert connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE participant_id = :id"),
                    {"id": cascade_participant_id},
                ) == 0
            assert connection.scalar(
                text(
                    "SELECT energy_consumption FROM daily_review_responses "
                    "WHERE id = :id"
                ),
                {"id": existing_review_id},
            ) == 7

            connection.execute(
                text(
                    """
                    INSERT INTO daily_review_responses (
                        id, participant_id, local_date, revision,
                        card_version, callback_event_id, submitted_at,
                        start_stress, start_energy, peak_stress, peak_period,
                        end_stress, end_energy, energy_consumption,
                        raw_json, created_at
                    ) VALUES (
                        :id, :participant_id, :local_date, 2,
                        'daily-review-v1', 'migration-optional-review',
                        :submitted_at, 3, 8, 9, 'evening', 5, 4, NULL,
                        '{}'::jsonb, :submitted_at
                    )
                    """
                ),
                {
                    "id": optional_review_id,
                    "participant_id": participant_id,
                    "local_date": date(2030, 1, 15),
                    "submitted_at": now + timedelta(minutes=5),
                },
            )
            assert connection.scalar(
                text(
                    "SELECT energy_consumption IS NULL "
                    "FROM daily_review_responses WHERE id = :id"
                ),
                {"id": optional_review_id},
            ) is True

            command.downgrade(config, "0020_oauth_refresh_lease")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0020_oauth_refresh_lease"
            downgraded_columns = {
                column["name"]: column
                for column in inspect(connection).get_columns(
                    "daily_review_responses"
                )
            }
            assert downgraded_columns["energy_consumption"]["nullable"] is False
            assert connection.scalar(
                text(
                    "SELECT energy_consumption FROM daily_review_responses "
                    "WHERE id = :id"
                ),
                {"id": optional_review_id},
            ) == 0

            command.upgrade(config, "0024_research_evaluation")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0024_research_evaluation"
    finally:
        config.attributes.pop("connection", None)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
