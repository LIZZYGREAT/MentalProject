"""Opt-in proof that the real 0016 -> current PostgreSQL migrations execute."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import build_engine
from app.services.research_evaluation import (
    DATASET_SCHEMA_V2,
    DATASET_SCHEMA_V3,
    DATASET_SCHEMA_V4,
    DATASET_SCHEMA_V7,
    ResearchEvaluationService,
)
from app.postgres_test_guard import (
    get_test_postgres_connect_timeout_seconds,
    optional_test_postgres_url,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[1]


class _ConnectionDatabase:
    """Run service sessions inside the migration test's isolated schema."""

    def __init__(self, connection):
        self.engine = connection.engine
        self.connection = connection

    @contextmanager
    def session(self):
        session = Session(
            bind=self.connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def test_real_postgres_upgrade_0016_to_head_preserves_and_backfills():
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
    engine = build_engine(
        raw_url,
        connect_timeout_seconds=get_test_postgres_connect_timeout_seconds(),
    )
    config = Config(str(RUNTIME_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(RUNTIME_ROOT / "migrations"))
    expected_head = ScriptDirectory.from_config(config).get_current_head()

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

            command.upgrade(config, "0025_dataset_snapshot_items")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0025_dataset_snapshot_items"
            inspector = inspect(connection)
            assert "forecast_currentness_events" in inspector.get_table_names()
            assert {
                "forecast_observation_matches",
                "dataset_snapshots",
                "dataset_snapshot_items",
                "model_evaluation_runs",
            } <= set(inspector.get_table_names())
            match_columns = {
                column["name"]
                for column in inspector.get_columns(
                    "forecast_observation_matches"
                )
            }
            assert "match_schema_version" in match_columns
            item_columns = {
                column["name"]
                for column in inspector.get_columns("dataset_snapshot_items")
            }
            assert {
                "dataset_snapshot_id", "item_type", "source_id",
                "source_version", "participant_id", "local_date",
                "source_hash", "metadata_json",
            } <= item_columns
            evaluation_columns = {
                column["name"]
                for column in inspector.get_columns("model_evaluation_runs")
            }
            assert {
                "evaluation_mode", "evaluation_code_version"
            } <= evaluation_columns
            item_unique = {
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints(
                    "dataset_snapshot_items"
                )
            }
            assert (
                "dataset_snapshot_id", "item_type", "source_id",
                "source_version",
            ) in item_unique
            item_foreign_keys = {
                tuple(item["constrained_columns"]): item
                for item in inspector.get_foreign_keys("dataset_snapshot_items")
            }
            assert item_foreign_keys[("participant_id",)]["options"].get(
                "ondelete"
            ) == "RESTRICT"
            assert item_foreign_keys[("dataset_snapshot_id",)]["options"].get(
                "ondelete"
            ) == "CASCADE"
            dataset_snapshot_id = uuid.uuid4()
            dataset_item_id = uuid.uuid4()
            legacy_metadata = {"observation_id": "source-1"}
            legacy_item = ResearchEvaluationService._item(
                "observation",
                "source-1",
                "observation.v1",
                participant_id,
                date(2030, 1, 15),
                legacy_metadata,
            )
            legacy_contract = {
                "schema_version": DATASET_SCHEMA_V2,
                "date_start": "2030-01-15",
                "date_end": "2030-01-15",
                "participant_filter": {
                    "participant_codes": ["MIGRATION-TEST"]
                },
                "observation_cutoff": now.isoformat(),
                "calendar_cutoff": now.isoformat(),
            }
            manifest = {
                "schema_version": DATASET_SCHEMA_V2,
                "participant_count": 1,
                "observation_count": 1,
                "forecast_count": 0,
                "calendar_count": 0,
                "item_count": 1,
                "manifest_hash": ResearchEvaluationService._manifest_hash(
                    legacy_contract, [legacy_item]
                ),
            }
            connection.execute(
                text(
                    """
                    INSERT INTO dataset_snapshots (
                        id, created_at, date_start, date_end,
                        participant_filter, observation_cutoff,
                        calendar_cutoff, schema_version, manifest_json
                    ) VALUES (
                        :id, :created_at, :date_start, :date_end,
                        CAST(:participant_filter AS jsonb),
                        :observation_cutoff, :calendar_cutoff,
                        :schema_version, CAST(:manifest AS jsonb)
                    )
                    """
                ),
                {
                    "id": dataset_snapshot_id,
                    "created_at": now,
                    "date_start": date(2030, 1, 15),
                    "date_end": date(2030, 1, 15),
                    "participant_filter": json.dumps(
                        {"participant_codes": ["MIGRATION-TEST"]}
                    ),
                    "observation_cutoff": now,
                    "calendar_cutoff": now,
                    "schema_version": DATASET_SCHEMA_V2,
                    "manifest": json.dumps(manifest),
                },
            )
            item_values = {
                "id": dataset_item_id,
                "dataset_snapshot_id": dataset_snapshot_id,
                "participant_id": participant_id,
                "local_date": date(2030, 1, 15),
                "source_hash": legacy_item["source_hash"],
                "metadata": json.dumps(legacy_metadata),
                "created_at": now,
            }
            connection.execute(
                text(
                    """
                    INSERT INTO dataset_snapshot_items (
                        id, dataset_snapshot_id, item_type, source_id,
                        source_version, participant_id, local_date,
                        source_hash, metadata_json, created_at
                    ) VALUES (
                        :id, :dataset_snapshot_id, 'observation', 'source-1',
                        'observation.v1', :participant_id, :local_date,
                        :source_hash,
                        CAST(:metadata AS jsonb), :created_at
                    )
                    """
                ),
                item_values,
            )
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM dataset_snapshot_items "
                    "WHERE dataset_snapshot_id = :id"
                ),
                {"id": dataset_snapshot_id},
            ) == manifest["item_count"]
            command.upgrade(config, "0026_dataset_participant_membership")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0026_dataset_participant_membership"
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
            connection.execute(
                text(
                    "INSERT INTO event_appraisal_feedback ("
                    "id, participant_id, event_id, mental_demand, "
                    "physical_demand, temporal_demand, effort, frustration, "
                    "perceived_control, actual_stress, perceived_performance, "
                    "submitted_at, created_at) VALUES ("
                    ":id, :participant_id, :event_id, :mental_demand, "
                    ":physical_demand, :temporal_demand, :effort, :frustration, "
                    ":perceived_control, :actual_stress, :perceived_performance, "
                    ":submitted_at, :created_at)"
                ),
                appraisal_values,
            )
            command.upgrade(config, "0027_workload_calibration")
            command.upgrade(config, "0028_workload_causal_provenance")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0028_workload_causal_provenance"

            appraisal_columns = {
                column["name"]
                for column in inspect(connection).get_columns(
                    "event_appraisal_feedback"
                )
            }
            assert {
                "event_type", "course_name", "workload_feature_vector",
                "workload_prior", "observed_workload", "workload_residual",
                "workload_model_version", "event_local_date", "event_start_at",
                "source_forecast_id", "source_forecast_version",
                "source_semantic_revision", "workload_schema_version",
            } <= appraisal_columns
            appraisal_checks = {
                item["name"]
                for item in inspect(connection).get_check_constraints(
                    "event_appraisal_feedback"
                )
            }
            assert {
                "ck_event_appraisal_workload_prior",
                "ck_event_appraisal_observed_workload",
            } <= appraisal_checks
            appraisal_indexes = {
                item["name"]
                for item in inspect(connection).get_indexes(
                    "event_appraisal_feedback"
                )
            }
            assert {
                "ix_event_appraisal_event_type",
                "ix_event_appraisal_course",
                "ix_event_appraisal_participant_event_date",
                "ix_event_appraisal_source_forecast",
            } <= appraisal_indexes
            appraisal_foreign_keys = {
                item["name"]: item
                for item in inspect(connection).get_foreign_keys(
                    "event_appraisal_feedback"
                )
            }
            assert appraisal_foreign_keys[
                "fk_event_appraisal_source_forecast"
            ]["referred_table"] == "forecast_snapshots"
            preserved_appraisal = connection.execute(
                text(
                    "SELECT mental_demand, physical_demand, temporal_demand, "
                    "effort, frustration, perceived_control, actual_stress, "
                    "perceived_performance, submitted_at, created_at, "
                    "source_forecast_id, source_forecast_version, "
                    "source_semantic_revision, workload_schema_version "
                    "FROM event_appraisal_feedback WHERE id = :id"
                ),
                {"id": appraisal_values["id"]},
            ).one()
            assert preserved_appraisal[:8] == (8, 2, 7, 8, 6, 4, 7, 6)
            assert preserved_appraisal[8] == now
            assert preserved_appraisal[9] == now
            assert preserved_appraisal[10:] == (None, None, None, None)
            assert connection.execute(
                text(
                    "SELECT schema_version, manifest_json "
                    "FROM dataset_snapshots WHERE id = :id"
                ),
                {"id": dataset_snapshot_id},
            ).one() == (DATASET_SCHEMA_V2, manifest)
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM dataset_snapshot_items "
                    "WHERE dataset_snapshot_id = :id"
                ),
                {"id": dataset_snapshot_id},
            ) == 1
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM dataset_snapshot_items "
                    "WHERE dataset_snapshot_id = :id "
                    "AND item_type = 'participant'"
                ),
                {"id": dataset_snapshot_id},
            ) == 0
            inspector = inspect(connection)
            item_checks = inspector.get_check_constraints(
                "dataset_snapshot_items"
            )
            assert any(
                "participant" in str(check.get("sqltext") or "")
                for check in item_checks
                if check.get("name") == "ck_dataset_snapshot_item_type"
            )
            command.upgrade(config, "0031_parameter_learning_runs")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0031_parameter_learning_runs"
            legacy_stage5_run = uuid.uuid4()
            old_promoted_stage5_run = uuid.uuid4()
            old_promoted_stage5_profile = uuid.uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO parameter_learning_runs (
                        id, participant_id, dataset_snapshot_id, model_family,
                        parameters_before, parameters_candidate,
                        training_metrics, validation_metrics, sample_count,
                        status, created_at
                    ) VALUES (
                        :id, :participant_id, :snapshot_id,
                        'hierarchical-ctssm-residual.v1', '{}'::jsonb,
                        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 30,
                        'candidate', :created_at
                    )
                    """
                ),
                {
                    "id": legacy_stage5_run,
                    "participant_id": participant_id,
                    "snapshot_id": dataset_snapshot_id,
                    "created_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO parameter_learning_runs (
                        id, participant_id, dataset_snapshot_id, model_family,
                        parameters_before, parameters_candidate,
                        training_metrics, validation_metrics, sample_count,
                        status, created_at
                    ) VALUES (
                        :id, :participant_id, :snapshot_id,
                        'hierarchical-ctssm-residual.v2', '{}'::jsonb,
                        '{"S_star_init": 60.0}'::jsonb, '{}'::jsonb,
                        '{"promotion_gate": {"version": "stage5-personalization-gate.v2", "passed": true}}'::jsonb,
                        30, 'promoted', :created_at
                    )
                    """
                ),
                {
                    "id": old_promoted_stage5_run,
                    "participant_id": participant_id,
                    "snapshot_id": dataset_snapshot_id,
                    "created_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO learned_model_profiles (
                        id, participant_id, version, parameters_json,
                        uncertainty_json, source, model_version,
                        validation_status, sample_count, day_count, confidence,
                        window_start, window_end, created_at
                    ) VALUES (
                        :id, :participant_id, 2,
                        jsonb_build_object(
                            'S_star_init', 60.0,
                            'model_selection', jsonb_build_object(
                                'status', 'stage5_promoted',
                                'parameter_learning_run_id', CAST(:run_id AS TEXT),
                                'active_variant', 'm1'
                            )
                        ),
                        '{}'::jsonb, 'stage5-old-promoted',
                        'mindflow-ctssm-runtime-v10', 'validated',
                        30, 14, 0.8, '2030-01-01', '2030-01-14', :created_at
                    )
                    """
                ),
                {
                    "id": old_promoted_stage5_profile,
                    "participant_id": participant_id,
                    "run_id": old_promoted_stage5_run,
                    "created_at": now,
                },
            )
            command.upgrade(config, "0034_dataset_v7_active_history")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0034_dataset_v7_active_history"
            assert connection.scalar(
                text(
                    "SELECT status FROM parameter_learning_runs WHERE id = :id"
                ),
                {"id": legacy_stage5_run},
            ) == "rejected"
            assert connection.scalar(
                text(
                    "SELECT status FROM parameter_learning_runs WHERE id = :id"
                ),
                {"id": old_promoted_stage5_run},
            ) == "rejected"
            assert connection.scalar(
                text(
                    "SELECT validation_status FROM learned_model_profiles "
                    "WHERE id = :id"
                ),
                {"id": old_promoted_stage5_profile},
            ) == "rejected"
            v11_v6_run = uuid.uuid4()
            v11_v6_profile = uuid.uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO parameter_learning_runs (
                        id, participant_id, dataset_snapshot_id, model_family,
                        run_kind, schedule_key, parameters_before,
                        parameters_candidate, training_metrics,
                        validation_metrics, sample_count, status, created_at
                    ) VALUES (
                        :id, :participant_id, :snapshot_id,
                        'hierarchical-ctssm-residual.v2', 'manual', NULL,
                        '{}'::jsonb, '{"S_star_init": 55.0}'::jsonb,
                        '{}'::jsonb,
                        '{"promotion_gate": {"version": "stage5-personalization-gate.v3", "passed": true, "formal_promotion_eligible": true}, "formal_replay_audit": {"engine": "stage5-real-ctssm-rolling-replay.v2"}}'::jsonb,
                        30, 'promoted', :created_at
                    )
                    """
                ),
                {
                    "id": v11_v6_run,
                    "participant_id": participant_id,
                    "snapshot_id": dataset_snapshot_id,
                    "created_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO learned_model_profiles (
                        id, participant_id, version, parameters_json,
                        uncertainty_json, source, model_version,
                        validation_status, sample_count, day_count, confidence,
                        window_start, window_end, created_at
                    ) VALUES (
                        :id, :participant_id, 3,
                        jsonb_build_object(
                            'S_star_init', 55.0,
                            'model_selection', jsonb_build_object(
                                'status', 'stage5_promoted',
                                'parameter_learning_run_id', CAST(:run_id AS TEXT),
                                'active_variant', 'm1'
                            )
                        ),
                        '{}'::jsonb, 'stage5-v11-pre-v7',
                        'mindflow-ctssm-runtime-v11', 'validated',
                        30, 14, 0.8, '2030-01-01', '2030-01-14', :created_at
                    )
                    """
                ),
                {
                    "id": v11_v6_profile,
                    "participant_id": participant_id,
                    "run_id": v11_v6_run,
                    "created_at": now,
                },
            )
            command.upgrade(config, "0035_stage5_v7_runtime_cutover")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0035_stage5_v7_runtime_cutover"
            assert connection.scalar(
                text(
                    "SELECT status FROM parameter_learning_runs WHERE id = :id"
                ),
                {"id": v11_v6_run},
            ) == "rejected"
            assert connection.scalar(
                text(
                    "SELECT validation_status FROM learned_model_profiles "
                    "WHERE id = :id"
                ),
                {"id": v11_v6_profile},
            ) == "rejected"
            command.upgrade(config, "0036_stage5_effective_profile")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0036_stage5_effective_profile"
            dataset_columns = {
                column["name"]
                for column in inspect(connection).get_columns(
                    "dataset_snapshots"
                )
            }
            assert {"purpose", "schedule_key"} <= dataset_columns
            assert connection.scalar(
                text(
                    "SELECT purpose FROM dataset_snapshots WHERE id = :id"
                ),
                {"id": dataset_snapshot_id},
            ) == "manual_research"
            dataset_indexes = {
                item["name"]: item
                for item in inspect(connection).get_indexes(
                    "dataset_snapshots"
                )
            }
            assert dataset_indexes[
                "uq_dataset_snapshot_weekly_batch"
            ]["unique"] is True
            stage5_columns = {
                column["name"]
                for column in inspect(connection).get_columns(
                    "parameter_learning_runs"
                )
            }
            assert {"run_kind", "schedule_key"} <= stage5_columns
            stage5_indexes = {
                item["name"]: item
                for item in inspect(connection).get_indexes(
                    "parameter_learning_runs"
                )
            }
            assert stage5_indexes[
                "uq_parameter_learning_scheduled_week"
            ]["unique"] is True
            scheduled_values = {
                "participant_id": participant_id,
                "snapshot_id": dataset_snapshot_id,
                "created_at": now,
            }
            scheduled_insert = text(
                """
                INSERT INTO parameter_learning_runs (
                    id, participant_id, dataset_snapshot_id, model_family,
                    run_kind, schedule_key, parameters_before,
                    parameters_candidate, training_metrics,
                    validation_metrics, sample_count, status, created_at
                ) VALUES (
                    :id, :participant_id, :snapshot_id,
                    'hierarchical-ctssm-residual.v2', 'scheduled',
                    '2030-W03', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                    '{}'::jsonb, 30, 'rejected', :created_at
                )
                """
            )
            connection.execute(
                scheduled_insert, {**scheduled_values, "id": uuid.uuid4()}
            )
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        scheduled_insert,
                        {**scheduled_values, "id": uuid.uuid4()},
                    )
            assert "model_promotion_decisions" in inspect(
                connection
            ).get_table_names()

            command.upgrade(config, "head")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == expected_head

            stage6_inspector = inspect(connection)
            warning_columns = {
                column["name"]: column
                for column in stage6_inspector.get_columns(
                    "warning_schedules"
                )
            }
            assert "authorization_deadline" in warning_columns
            assert warning_columns["authorization_deadline"]["nullable"] is False
            for warning_id in (
                source_warning_id,
                child_warning_id,
                invalid_warning_id,
            ):
                authorization_deadline, risk_time = connection.execute(
                    text(
                        "SELECT authorization_deadline, risk_time "
                        "FROM warning_schedules WHERE id = :id"
                    ),
                    {"id": warning_id},
                ).one()
                assert authorization_deadline == risk_time

            care_preference_columns = {
                column["name"]
                for column in stage6_inspector.get_columns(
                    "participant_care_preferences"
                )
            }
            assert {
                "inferred_support_types",
                "disabled_intervention_types",
                "interruption_tolerance",
                "preferred_reminder_windows",
            } <= care_preference_columns

            care_event_columns = {
                column["name"]
                for column in stage6_inspector.get_columns(
                    "care_intervention_events"
                )
            }
            assert {
                "vulnerability_score",
                "receptivity_score",
                "decision_score",
                "decision_json",
            } <= care_event_columns

            stage6_tables = set(stage6_inspector.get_table_names())
            assert {
                "care_intervention_outcomes",
                "intervention_randomization_events",
            } <= stage6_tables
            outcome_foreign_keys = stage6_inspector.get_foreign_keys(
                "care_intervention_outcomes"
            )
            assert any(
                key["constrained_columns"] == ["intervention_id"]
                and key["referred_table"] == "care_intervention_events"
                and str((key.get("options") or {}).get("ondelete")).upper()
                == "CASCADE"
                for key in outcome_foreign_keys
            )
            assert any(
                key["constrained_columns"] == ["participant_id"]
                and key["referred_table"] == "participants"
                for key in stage6_inspector.get_foreign_keys(
                    "intervention_randomization_events"
                )
            )

            service = ResearchEvaluationService(
                _ConnectionDatabase(connection), "Asia/Shanghai"
            )
            legacy_run = service.create_evaluation_run(
                dataset_snapshot_id, "algorithm-v1"
            )
            legacy_participant_run = service.create_evaluation_run(
                dataset_snapshot_id,
                "algorithm-v1",
                participant_id=participant_id,
            )
            assert legacy_run["status"] == "completed"
            assert legacy_run["metrics"]["metrics"]["sample_count"] == 0
            assert legacy_run["metrics"]["config"][
                "dataset_schema_version"
            ] == DATASET_SCHEMA_V2
            assert legacy_participant_run["status"] == "completed"

            promotion_inspector = inspect(connection)
            promotion_fks = {
                item["referred_table"]: item
                for item in promotion_inspector.get_foreign_keys(
                    "model_promotion_decisions"
                )
            }
            assert {
                "model_evaluation_runs",
                "dataset_snapshots",
                "participants",
            } <= set(promotion_fks)
            assert all(
                str((promotion_fks[table].get("options") or {}).get("ondelete")).upper()
                == "RESTRICT"
                for table in (
                    "model_evaluation_runs",
                    "dataset_snapshots",
                    "participants",
                )
            )
            assert any(
                item.get("name") == "ck_model_promotion_status"
                and "retained_from_empirical_evidence"
                in str(item.get("sqltext") or "")
                for item in promotion_inspector.get_check_constraints(
                    "model_promotion_decisions"
                )
            )
            promotion_indexes = {
                item["name"]
                for item in promotion_inspector.get_indexes(
                    "model_promotion_decisions"
                )
            }
            assert {
                "ix_model_promotion_participant_promoted",
                "uq_model_promotion_cohort_run_family",
            } <= promotion_indexes

            def insert_promotion(
                decision_id, run_id, promoted_participant, family="m1",
                status="retained_from_empirical_evidence",
            ):
                connection.execute(
                    text(
                        "INSERT INTO model_promotion_decisions ("
                        "id, model_evaluation_run_id, dataset_snapshot_id, "
                        "participant_id, model_family, promotion_gate_version, "
                        "evaluation_code_version, parameters_hash, status, "
                        "passed_at, promoted_at) VALUES ("
                        ":id, :run_id, :snapshot_id, :participant_id, :family, "
                        "'gate.v1', 'evaluation.v1', :parameters_hash, :status, "
                        ":passed_at, :promoted_at)"
                    ),
                    {
                        "id": decision_id,
                        "run_id": uuid.UUID(run_id),
                        "snapshot_id": dataset_snapshot_id,
                        "participant_id": promoted_participant,
                        "family": family,
                        "parameters_hash": uuid.uuid4().hex,
                        "status": status,
                        "passed_at": now,
                        "promoted_at": now,
                    },
                )

            participant_decision_id = uuid.uuid4()
            insert_promotion(
                participant_decision_id,
                legacy_participant_run["id"],
                participant_id,
            )
            with pytest.raises(IntegrityError), connection.begin_nested():
                insert_promotion(
                    uuid.uuid4(),
                    legacy_participant_run["id"],
                    participant_id,
                )
            insert_promotion(
                uuid.uuid4(), legacy_run["id"], None, family="m2"
            )
            with pytest.raises(IntegrityError), connection.begin_nested():
                insert_promotion(
                    uuid.uuid4(), legacy_run["id"], None, family="m2"
                )
            with pytest.raises(IntegrityError), connection.begin_nested():
                insert_promotion(
                    uuid.uuid4(),
                    legacy_participant_run["id"],
                    participant_id,
                    family="m3",
                    status="invalid",
                )
            for table, identifier in (
                ("model_evaluation_runs", uuid.UUID(legacy_participant_run["id"])),
                ("dataset_snapshots", dataset_snapshot_id),
                ("participants", participant_id),
            ):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE id = :id"),
                        {"id": identifier},
                    )

            explicit_profile_id = uuid.uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO participant_profiles (
                        id, participant_id, version, profile_json, created_at
                    ) VALUES (
                        :id, :participant_id, 1,
                        '{"model_params": {"S_star_init": 46.0}}'::jsonb,
                        :created_at
                    )
                    """
                ),
                {
                    "id": explicit_profile_id,
                    "participant_id": participant_id,
                    "created_at": now - timedelta(days=1),
                },
            )
            v3_snapshot = service.create_dataset_snapshot(
                date_start=date(2030, 1, 15),
                date_end=date(2030, 1, 15),
                participant_filter={
                    "participant_codes": ["MIGRATION-TEST"]
                },
                observation_cutoff=now,
                calendar_cutoff=now,
            )
            assert v3_snapshot["schema_version"] == DATASET_SCHEMA_V7
            assert v3_snapshot["manifest"]["participant_count"] == 1
            assert v3_snapshot["manifest"]["participant_profile_count"] == 1
            assert v3_snapshot["manifest"]["learned_model_profile_count"] == 3
            v3_snapshot_id = uuid.UUID(v3_snapshot["id"])
            memberships = service.snapshot_items(v3_snapshot_id, "participant")
            assert len(memberships) == 1
            membership = memberships[0]
            assert membership["participant_id"] == str(participant_id)
            frozen_profiles = service.snapshot_items(
                v3_snapshot_id, "participant_profile"
            )
            assert len(frozen_profiles) == 1
            assert frozen_profiles[0]["metadata"]["profile_id"] == str(
                explicit_profile_id
            )
            frozen_learned_profiles = service.snapshot_items(
                v3_snapshot_id, "learned_model_profile"
            )
            assert len(frozen_learned_profiles) == 3
            rejected_frozen = next(
                item
                for item in frozen_learned_profiles
                if item["metadata"]["profile_id"]
                == str(old_promoted_stage5_profile)
            )
            assert rejected_frozen["metadata"]["runtime_valid"] is False
            v3_run = service.create_evaluation_run(
                v3_snapshot_id,
                "algorithm-v1",
                participant_id=participant_id,
            )
            assert v3_run["status"] == "completed"
            assert v3_run["metrics"]["metrics"]["sample_count"] == 0

            with pytest.raises(IntegrityError), connection.begin_nested():
                duplicate = dict(item_values)
                duplicate["id"] = uuid.uuid4()
                connection.execute(
                    text(
                        """
                        INSERT INTO dataset_snapshot_items (
                            id, dataset_snapshot_id, item_type, source_id,
                            source_version, participant_id, local_date,
                            source_hash, metadata_json, created_at
                        ) VALUES (
                            :id, :dataset_snapshot_id, 'observation', 'source-1',
                            'observation.v1', :participant_id, :local_date,
                            'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                            CAST(:metadata AS jsonb), :created_at
                        )
                        """
                    ),
                    duplicate,
                )
            membership_values = {
                "id": uuid.UUID(membership["id"]),
                "dataset_snapshot_id": v3_snapshot_id,
                "participant_id": participant_id,
                "local_date": date.fromisoformat(membership["local_date"]),
                "source_hash": membership["source_hash"],
                "metadata": json.dumps(membership["metadata"]),
                "created_at": now,
            }
            with pytest.raises(IntegrityError), connection.begin_nested():
                duplicate_membership = dict(membership_values)
                duplicate_membership["id"] = uuid.uuid4()
                connection.execute(
                    text(
                        """
                        INSERT INTO dataset_snapshot_items (
                            id, dataset_snapshot_id, item_type, source_id,
                            source_version, participant_id, local_date,
                            source_hash, metadata_json, created_at
                        ) VALUES (
                            :id, :dataset_snapshot_id, 'participant',
                            CAST(:participant_id AS text),
                            'participant-membership.v1', :participant_id,
                            :local_date,
                            :source_hash,
                            CAST(:metadata AS jsonb), :created_at
                        )
                        """
                    ),
                    duplicate_membership,
                )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    text("DELETE FROM participants WHERE id = :id"),
                    {"id": participant_id},
                )
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

            command.upgrade(config, "0030_model_promotion_decisions")
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "0030_model_promotion_decisions"
    finally:
        config.attributes.pop("connection", None)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_real_postgres_upgrade_0040_to_0041_repairs_legacy_course_schedule_writes():
    try:
        raw_url = optional_test_postgres_url()
    except ValueError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not configured")

    schema = f"mindflow_course_schedule_migration_{uuid.uuid4().hex}"
    participant_id = uuid.uuid4()
    invalid_import_id = uuid.uuid4()
    invalid_item_id = uuid.uuid4()
    invalid_write_id = uuid.uuid4()
    valid_import_id = uuid.uuid4()
    valid_item_id = uuid.uuid4()
    valid_write_id = uuid.uuid4()
    now = datetime(2030, 1, 15, 2, 0, tzinfo=timezone.utc)
    engine = build_engine(
        raw_url,
        connect_timeout_seconds=get_test_postgres_connect_timeout_seconds(),
    )
    config = Config(str(RUNTIME_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(RUNTIME_ROOT / "migrations"))

    def insert_import(connection, import_id, source_message_id, completed_at):
        connection.execute(
            text(
                """
                INSERT INTO course_schedule_imports (
                    id, participant_id, source_message_id, source_image_hash,
                    status, semester_start_date, timezone, vision_model,
                    structured_result, created_at, expires_at, confirmed_at,
                    recurrence_strategy, recurrence_confirmed_at,
                    completed_at, last_progress_at
                ) VALUES (
                    :id, :participant_id, :source_message_id, :source_image_hash,
                    'succeeded', '2030-01-14', 'Asia/Shanghai', 'vision-model',
                    '{}'::jsonb, :created_at, :expires_at, :confirmed_at,
                    'preserve_schedule_pattern', :confirmed_at,
                    :completed_at, :last_progress_at
                )
                """
            ),
            {
                "id": import_id,
                "participant_id": participant_id,
                "source_message_id": source_message_id,
                "source_image_hash": uuid.uuid4().hex * 2,
                "created_at": now,
                "expires_at": now + timedelta(days=1),
                "confirmed_at": now,
                "completed_at": completed_at,
                "last_progress_at": completed_at,
            },
        )

    def insert_item_and_write(
        connection, import_id, item_id, write_id, source_identity, provider_event_id
    ):
        connection.execute(
            text(
                """
                INSERT INTO course_schedule_import_items (
                    id, import_id, item_index, course_name, weekday,
                    start_time, end_time, location, week_rule_json,
                    normalized_key, status
                ) VALUES (
                    :id, :import_id, 0, '迁移测试课程', 1,
                    TIME '08:00', TIME '09:35', 'A101',
                    '{}'::jsonb, :normalized_key, 'succeeded'
                )
                """
            ),
            {
                "id": item_id,
                "import_id": import_id,
                "normalized_key": f"course-{item_id}",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO course_schedule_import_writes (
                    id, import_id, item_id, occurrence_identity,
                    source_identity, write_kind, summary, description,
                    start_time, end_time, affected_dates_json, status,
                    provider_event_id, error_code, created_at, updated_at
                ) VALUES (
                    :id, :import_id, :item_id, 'week-1', :source_identity,
                    'single', '迁移测试课程', '', :created_at, :end_time,
                    '[\"2030-01-14\"]'::jsonb, 'created',
                    :provider_event_id, NULL, :created_at, :created_at
                )
                """
            ),
            {
                "id": write_id,
                "import_id": import_id,
                "item_id": item_id,
                "source_identity": source_identity,
                "created_at": now,
                "end_time": now + timedelta(hours=1),
                "provider_event_id": provider_event_id,
            },
        )

    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config.attributes["connection"] = connection
            command.upgrade(config, "0040_course_schedule_import_ledger")
            connection.execute(
                text(
                    "INSERT INTO participants (id, participant_code) "
                    "VALUES (:id, 'COURSE-SCHEDULE-MIGRATION')"
                ),
                {"id": participant_id},
            )
            insert_import(connection, invalid_import_id, "legacy-invalid", now)
            insert_item_and_write(
                connection,
                invalid_import_id,
                invalid_item_id,
                invalid_write_id,
                "legacy-invalid-source",
                None,
            )
            insert_import(connection, valid_import_id, "legacy-valid", now)
            insert_item_and_write(
                connection,
                valid_import_id,
                valid_item_id,
                valid_write_id,
                "legacy-valid-source",
                "evt-valid",
            )

            command.upgrade(config, "0041_course_schedule_created_provider_id_guard")

            invalid_write = connection.execute(
                text(
                    "SELECT status, provider_event_id, error_code "
                    "FROM course_schedule_import_writes WHERE id = :id"
                ),
                {"id": invalid_write_id},
            ).one()
            assert invalid_write == (
                "create_outcome_unknown",
                None,
                "legacy_missing_provider_event_id",
            )
            invalid_import = connection.execute(
                text(
                    "SELECT status, completed_at FROM course_schedule_imports "
                    "WHERE id = :id"
                ),
                {"id": invalid_import_id},
            ).one()
            assert invalid_import.status == "partial_failed"
            assert invalid_import.completed_at is None

            valid_write = connection.execute(
                text(
                    "SELECT status, provider_event_id "
                    "FROM course_schedule_import_writes WHERE id = :id"
                ),
                {"id": valid_write_id},
            ).one()
            assert valid_write == ("created", "evt-valid")

            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "UPDATE course_schedule_import_writes "
                            "SET status = 'created', provider_event_id = NULL "
                            "WHERE id = :id"
                        ),
                        {"id": invalid_write_id},
                    )
    finally:
        config.attributes.pop("connection", None)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


def test_real_postgres_upgrade_0043_to_0045_backfills_compensation_refresh_outbox():
    try:
        raw_url = optional_test_postgres_url()
    except ValueError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not configured")

    schema = f"mindflow_course_schedule_refresh_{uuid.uuid4().hex}"
    participant_id = uuid.uuid4()
    import_id = uuid.uuid4()
    item_a = uuid.uuid4()
    item_b = uuid.uuid4()
    write_a = uuid.uuid4()
    write_b = uuid.uuid4()
    now = datetime(2030, 1, 15, 2, 0, tzinfo=timezone.utc)
    engine = build_engine(
        raw_url,
        connect_timeout_seconds=get_test_postgres_connect_timeout_seconds(),
    )
    config = Config(str(RUNTIME_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(RUNTIME_ROOT / "migrations"))

    try:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config.attributes["connection"] = connection
            command.upgrade(config, "0043_course_schedule_completion_presentation")
            connection.execute(
                text(
                    "INSERT INTO participants (id, participant_code) "
                    "VALUES (:id, :participant_code)"
                ),
                {
                    "id": participant_id,
                    "participant_code": "COURSE-REFRESH-MIGRATION",
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO course_schedule_imports (
                        id, participant_id, source_message_id, source_image_hash,
                        status, semester_start_date, timezone, vision_model,
                        structured_result, created_at, expires_at, confirmed_at,
                        recurrence_strategy, recurrence_confirmed_at,
                        completed_at, last_progress_at
                    ) VALUES (
                        :id, :participant_id, 'refresh-migration', :source_hash,
                        'succeeded', '2030-01-14', 'Asia/Shanghai', 'vision-model',
                        '{}'::jsonb, :created_at, :expires_at, :confirmed_at,
                        'preserve_schedule_pattern', :confirmed_at,
                        :completed_at, :last_progress_at
                    )
                    """
                ),
                {
                    "id": import_id,
                    "participant_id": participant_id,
                    "source_hash": uuid.uuid4().hex * 2,
                    "created_at": now,
                    "expires_at": now + timedelta(days=1),
                    "confirmed_at": now,
                    "completed_at": now,
                    "last_progress_at": now,
                },
            )
            for item_id, index, normalized_key in (
                (item_a, 0, "refresh-course-a"),
                (item_b, 1, "refresh-course-b"),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO course_schedule_import_items (
                            id, import_id, item_index, course_name, weekday,
                            start_time, end_time, location, week_rule_json,
                            normalized_key, status
                        ) VALUES (
                            :id, :import_id, :item_index, '迁移课程', 1,
                            TIME '08:00', TIME '09:35', 'A101',
                            '{}'::jsonb, :normalized_key, 'succeeded'
                        )
                        """
                    ),
                    {
                        "id": item_id,
                        "import_id": import_id,
                        "item_index": index,
                        "normalized_key": normalized_key,
                    },
                )
            for write_id, item_id, occurrence, source, status, primary, conflict in (
                (
                    write_a,
                    item_a,
                    "week-1-a",
                    "refresh-source-a",
                    "delete_failed",
                    "evt-a",
                    "evt-b",
                ),
                (
                    write_b,
                    item_b,
                    "week-1-b",
                    "refresh-source-b",
                    "deleted",
                    "evt-c",
                    None,
                ),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO course_schedule_import_writes (
                            id, import_id, item_id, occurrence_identity,
                            source_identity, write_kind, summary, description,
                            start_time, end_time, affected_dates_json, status,
                            provider_event_id, provider_conflict_event_id,
                            error_code, created_at, updated_at
                        ) VALUES (
                            :id, :import_id, :item_id, :occurrence,
                            :source, 'single', '迁移课程', '', :start_time,
                            :end_time, '["2030-01-14"]'::jsonb, :status,
                            :primary, :conflict, NULL, :start_time, :start_time
                        )
                        """
                    ),
                    {
                        "id": write_id,
                        "import_id": import_id,
                        "item_id": item_id,
                        "occurrence": occurrence,
                        "source": source,
                        "status": status,
                        "primary": primary,
                        "conflict": conflict,
                        "start_time": now,
                        "end_time": now + timedelta(hours=1),
                    },
                )

            command.upgrade(config, "0045_course_schedule_compensation_refresh_outbox")
            rows = connection.execute(
                text(
                    "SELECT provider_event_id, provider_identity_kind, status, "
                    "rollback_refresh_status FROM course_schedule_import_compensations "
                    "WHERE import_id = :import_id ORDER BY provider_event_id"
                ),
                {"import_id": import_id},
            ).all()
            assert rows == [
                ("evt-a", "primary", "delete_failed", None),
                ("evt-b", "conflict", "delete_failed", None),
                ("evt-c", "primary", "deleted", "pending"),
            ]
            assert connection.scalar(
                text(
                    "SELECT status FROM course_schedule_imports WHERE id = :id"
                ),
                {"id": import_id},
            ) == "cleanup_failed"
            assert connection.scalar(
                text(
                    "SELECT COUNT(*) FROM course_schedule_import_writes "
                    "WHERE import_id = :id AND status = 'created'"
                ),
                {"id": import_id},
            ) == 2
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            INSERT INTO course_schedule_import_compensations (
                                id, import_id, write_id, participant_id,
                                provider_event_id, provider_identity_kind,
                                affected_dates_json, status, attempt_count,
                                created_at, updated_at
                            ) VALUES (
                                :id, :import_id, :write_id, :participant_id,
                                'evt-c', 'primary', '[]'::jsonb, 'deleted', 0,
                                :created_at, :updated_at
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "import_id": import_id,
                            "write_id": write_b,
                            "participant_id": participant_id,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
    finally:
        config.attributes.pop("connection", None)
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()
