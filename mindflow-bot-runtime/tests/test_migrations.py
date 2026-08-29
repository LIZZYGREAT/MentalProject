from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
VERSION_NUM_CAPACITY = 64


def _migration(path: Path):
    spec = spec_from_file_location(f"test_migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0008_expands_alembic_version_before_business_schema(monkeypatch):
    migration = _migration(VERSIONS / "0008_forecast_observation_revision.py")
    statements = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert statements[0] == (
        "ALTER TABLE alembic_version "
        "ALTER COLUMN version_num TYPE VARCHAR(64)"
    )
    assert "ADD COLUMN observation_revision VARCHAR(64)" in statements[1]
    assert all("TYPE VARCHAR(32)" not in statement for statement in statements)

    statements.clear()
    migration.downgrade()
    assert all("alembic_version" not in statement for statement in statements)


def test_migration_revision_ids_fit_alembic_version_capacity():
    migrations = [_migration(path) for path in sorted(VERSIONS.glob("*.py"))]
    revisions = [migration.revision for migration in migrations]

    assert max(map(len, revisions)) <= VERSION_NUM_CAPACITY
    assert "0008_forecast_observation_revision" in revisions
    migration_0009 = next(
        migration for migration in migrations
        if migration.revision == "0009_learned_model_profiles"
    )
    assert migration_0009.down_revision == "0008_forecast_observation_revision"
    migration_0010 = next(
        migration for migration in migrations
        if migration.revision == "0010_response_delivery"
    )
    assert migration_0010.down_revision == "0009_learned_model_profiles"
    assert len(migration_0010.revision) <= 32
    migration_0014 = next(
        migration for migration in migrations
        if migration.revision == "0014_daily_review_expiry"
    )
    assert migration_0014.down_revision == "0013_daily_review_feedback"
    migration_0015 = next(
        migration for migration in migrations
        if migration.revision == "0015_daily_review_causal_source"
    )
    assert migration_0015.down_revision == "0014_daily_review_expiry"
    migration_0018 = next(
        migration for migration in migrations
        if migration.revision == "0018_calendar_mutation_reconciliation"
    )
    assert migration_0018.down_revision == "0017_care_delivery_authorization"
    migration_0019 = next(
        migration for migration in migrations
        if migration.revision == "0019_forecast_currentness_history"
    )
    assert migration_0019.down_revision == "0018_calendar_mutation_reconciliation"
    migration_0020 = next(
        migration for migration in migrations
        if migration.revision == "0020_oauth_refresh_lease"
    )
    assert migration_0020.down_revision == "0019_forecast_currentness_history"
    migration_0021 = next(
        migration for migration in migrations
        if migration.revision == "0021_daily_review_energy_optional"
    )
    assert migration_0021.down_revision == "0020_oauth_refresh_lease"
    migration_0022 = next(
        migration for migration in migrations
        if migration.revision == "0022_research_profile_v2"
    )
    assert migration_0022.down_revision == "0021_daily_review_energy_optional"
    migration_0023 = next(
        migration for migration in migrations
        if migration.revision == "0023_stage1_gate_constraints"
    )
    assert migration_0023.down_revision == "0022_research_profile_v2"
    migration_0024 = next(
        migration for migration in migrations
        if migration.revision == "0024_research_evaluation"
    )
    assert migration_0024.down_revision == "0023_stage1_gate_constraints"
    migration_0025 = next(
        migration for migration in migrations
        if migration.revision == "0025_dataset_snapshot_items"
    )
    assert migration_0025.down_revision == "0024_research_evaluation"
    migration_0026 = next(
        migration for migration in migrations
        if migration.revision == "0026_dataset_participant_membership"
    )
    assert migration_0026.down_revision == "0025_dataset_snapshot_items"
    migration_0027 = next(
        migration for migration in migrations
        if migration.revision == "0027_workload_calibration"
    )
    assert migration_0027.down_revision == "0026_dataset_participant_membership"
    migration_0028 = next(
        migration for migration in migrations
        if migration.revision == "0028_workload_causal_provenance"
    )
    assert migration_0028.down_revision == "0027_workload_calibration"


def test_0021_makes_energy_consumption_nullable_and_has_safe_downgrade(
    monkeypatch,
):
    migration = _migration(
        VERSIONS / "0021_daily_review_energy_optional.py"
    )
    alterations = []
    statements = []
    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda *args, **kwargs: alterations.append((args, kwargs)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()
    assert alterations[-1][0] == (
        "daily_review_responses",
        "energy_consumption",
    )
    assert alterations[-1][1]["nullable"] is True

    migration.downgrade()
    assert "WHERE energy_consumption IS NULL" in statements[-1]
    assert alterations[-1][1]["nullable"] is False


def test_0022_adds_stage1_research_tables_and_learned_parameter_audit(monkeypatch):
    migration = _migration(VERSIONS / "0022_research_profile_v2.py")
    tables = []
    columns = []
    indexes = []
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *items, **kwargs: tables.append(
            (name, {item.name for item in items if hasattr(item, "name")})
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, table, fields, **kwargs: indexes.append(
            (name, table, tuple(fields))
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: columns.append((table, column)),
    )

    migration.upgrade()

    table_map = dict(tables)
    assert set(table_map) == {
        "psychometric_assessments",
        "participant_slow_states",
        "event_appraisal_feedback",
    }
    assert {"instrument_name", "raw_items_json", "scores_json"} <= table_map[
        "psychometric_assessments"
    ]
    assert {"rolling_7d_workload", "recent_sleep_debt"} <= table_map[
        "participant_slow_states"
    ]
    assert {"mental_demand", "actual_stress", "perceived_control"} <= table_map[
        "event_appraisal_feedback"
    ]
    assert [column.name for table, column in columns if table == "learned_model_profiles"] == [
        "uncertainty_json",
        "model_version",
        "validation_status",
    ]
    assert len(indexes) == 3


def test_0023_adds_learned_and_slow_state_checks_without_relabeling_legacy(
    monkeypatch,
):
    migration = _migration(VERSIONS / "0023_stage1_gate_constraints.py")
    created = []
    statements = []
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: created.append((name, table, condition)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert len(created) == 11
    assert {
        "ck_learned_profile_validation_status",
        "ck_learned_profile_sample_count",
        "ck_learned_profile_day_count",
        "ck_learned_profile_confidence",
        "ck_learned_profile_window",
    } <= {name for name, table, _ in created if table == "learned_model_profiles"}
    assert {
        "ck_slow_state_cadence",
        "ck_slow_state_stress",
        "ck_slow_state_workload",
        "ck_slow_state_energy",
        "ck_slow_state_recovery",
        "ck_slow_state_sleep_debt",
    } <= {name for name, table, _ in created if table == "participant_slow_states"}
    assert statements == []


def test_0024_adds_reproducible_research_evaluation_tables(monkeypatch):
    migration = _migration(VERSIONS / "0024_research_evaluation.py")
    tables = []
    indexes = []
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *items, **kwargs: tables.append(
            (name, {item.name for item in items if hasattr(item, "name")})
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, table, fields, **kwargs: indexes.append(
            (name, table, tuple(fields))
        ),
    )

    migration.upgrade()

    table_map = dict(tables)
    assert set(table_map) == {
        "forecast_observation_matches",
        "dataset_snapshots",
        "model_evaluation_runs",
    }
    assert {
        "forecast_version",
        "forecast_timestamp",
        "observation_id",
        "predicted_stress",
        "actual_stress",
        "residual",
        "context_json",
    } <= table_map["forecast_observation_matches"]
    assert {
        "participant_filter",
        "observation_cutoff",
        "calendar_cutoff",
        "schema_version",
        "manifest_json",
    } <= table_map["dataset_snapshots"]
    assert {
        "dataset_snapshot_id",
        "model_version",
        "participant_id",
        "metrics_json",
        "status",
    } <= table_map["model_evaluation_runs"]
    assert len(indexes) == 4


def test_0025_freezes_snapshot_items_and_evaluation_modes(monkeypatch):
    migration = _migration(VERSIONS / "0025_dataset_snapshot_items.py")
    columns = []
    tables = []
    indexes = []
    unique_constraints = []
    checks = []
    statements = []
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: columns.append((table, column)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)
    monkeypatch.setattr(migration.op, "alter_column", lambda *a, **k: None)
    monkeypatch.setattr(migration.op, "drop_constraint", lambda *a, **k: None)
    monkeypatch.setattr(
        migration.op,
        "create_unique_constraint",
        lambda name, table, fields: unique_constraints.append(
            (name, table, tuple(fields))
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *items, **kwargs: tables.append(
            (name, {item.name for item in items if hasattr(item, "name")})
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, table, fields, **kwargs: indexes.append(
            (name, table, tuple(fields))
        ),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )

    migration.upgrade()

    assert [column.name for table, column in columns] == [
        "match_schema_version",
        "evaluation_mode",
        "evaluation_code_version",
    ]
    assert "PARTITION BY observation_id" in statements[1]
    assert unique_constraints == [
        (
            "uq_forecast_observation_match_schema",
            "forecast_observation_matches",
            ("observation_id", "match_schema_version"),
        )
    ]
    table_map = dict(tables)
    assert set(table_map) == {"dataset_snapshot_items"}
    assert {
        "dataset_snapshot_id",
        "item_type",
        "source_id",
        "source_version",
        "participant_id",
        "local_date",
        "source_hash",
        "metadata_json",
    } <= table_map["dataset_snapshot_items"]
    assert len(indexes) == 2
    assert {name for name, _, _ in checks} == {
        "ck_model_evaluation_status",
        "ck_model_evaluation_mode",
    }


def test_0026_allows_frozen_participant_membership(monkeypatch):
    migration = _migration(
        VERSIONS / "0026_dataset_participant_membership.py"
    )
    dropped = []
    checks = []
    statements = []
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, table, **kwargs: dropped.append((name, table, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert dropped == [
        (
            "ck_dataset_snapshot_item_type",
            "dataset_snapshot_items",
            {"type_": "check"},
        )
    ]
    assert len(checks) == 1
    assert "'participant'" in checks[0][2]
    assert statements == []

    dropped.clear()
    checks.clear()
    migration.downgrade()
    assert statements == [
        "DELETE FROM dataset_snapshot_items WHERE item_type = 'participant'"
    ]
    assert "'participant'" not in checks[0][2]


def test_0027_adds_workload_calibration_fields_and_constraints(monkeypatch):
    migration = _migration(VERSIONS / "0027_workload_calibration.py")
    columns = []
    checks = []
    indexes = []
    monkeypatch.setattr(
        migration.op, "add_column",
        lambda table, column: columns.append((table, column.name)),
    )
    monkeypatch.setattr(
        migration.op, "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )
    monkeypatch.setattr(
        migration.op, "create_index",
        lambda name, table, fields: indexes.append((name, table, tuple(fields))),
    )

    migration.upgrade()

    assert [name for _, name in columns] == [
        "event_type",
        "course_name",
        "workload_feature_vector",
        "workload_prior",
        "observed_workload",
        "workload_residual",
        "workload_model_version",
    ]
    assert {name for name, _, _ in checks} == {
        "ck_event_appraisal_workload_prior",
        "ck_event_appraisal_observed_workload",
    }
    assert {name for name, _, _ in indexes} == {
        "ix_event_appraisal_event_type",
        "ix_event_appraisal_course",
    }


def test_0028_adds_nullable_causal_provenance_fk_and_indexes(monkeypatch):
    migration = _migration(VERSIONS / "0028_workload_causal_provenance.py")
    columns = []
    foreign_keys = []
    indexes = []
    monkeypatch.setattr(
        migration.op, "add_column",
        lambda table, column: columns.append((table, column)),
    )
    monkeypatch.setattr(
        migration.op, "create_foreign_key",
        lambda *args, **kwargs: foreign_keys.append((args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op, "create_index",
        lambda name, table, fields: indexes.append((name, table, tuple(fields))),
    )

    migration.upgrade()

    assert [column.name for _, column in columns] == [
        "event_local_date",
        "event_start_at",
        "source_forecast_id",
        "source_forecast_version",
        "source_semantic_revision",
        "workload_schema_version",
    ]
    assert all(column.nullable for _, column in columns)
    assert foreign_keys == [(
        (
            "fk_event_appraisal_source_forecast",
            "event_appraisal_feedback",
            "forecast_snapshots",
            ["source_forecast_id"],
            ["id"],
        ),
        {"ondelete": "SET NULL"},
    )]
    assert {name for name, _, _ in indexes} == {
        "ix_event_appraisal_participant_event_date",
        "ix_event_appraisal_source_forecast",
    }


def test_0015_backfills_causal_source_without_guessing_orphan_responses(
    monkeypatch,
):
    migration = _migration(
        VERSIONS / "0015_daily_review_causal_source.py"
    )
    columns = []
    foreign_keys = []
    statements = []
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: columns.append((table, column)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_foreign_key",
        lambda *args, **kwargs: foreign_keys.append((args, kwargs)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert [column.name for _, column in columns] == [
        "causal_source_forecast_id",
        "causal_source_forecast_version",
    ]
    assert all(column.nullable for _, column in columns)
    assert foreign_keys[0][1]["ondelete"] == "RESTRICT"
    statement = statements[0]
    assert "DISTINCT ON (daily_review_response_id)" in statement
    assert "generated_at ASC, id ASC" in statement
    assert "response.causal_source_forecast_id IS NULL" in statement


def test_0017_adds_calendar_state_and_safe_snooze_backfill(monkeypatch):
    migration = _migration(
        VERSIONS / "0017_care_delivery_authorization.py"
    )
    columns = []
    statements = []
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: columns.append((table, column)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)
    monkeypatch.setattr(migration.op, "create_foreign_key", lambda *a, **k: None)
    monkeypatch.setattr(
        migration.op, "create_unique_constraint", lambda *a, **k: None
    )
    monkeypatch.setattr(migration.op, "alter_column", lambda *a, **k: None)

    migration.upgrade()

    assert columns[0][0] == "calendar_snapshots"
    assert columns[0][1].name == "snapshot_state"
    assert columns[0][1].nullable is False
    calendar_backfill = next(
        statement for statement in statements
        if "UPDATE calendar_snapshots" in statement
    )
    assert "snapshot_state = 'provider_degraded'" in calendar_backfill
    assert "WHERE degraded = true" in calendar_backfill
    snooze_backfill = next(
        statement for statement in statements
        if "snoozed_from_intervention_id = candidate.intervention_id" in statement
    )
    assert "intervention.id::text" in snooze_backfill
    assert "candidate.candidate_rank = 1" in snooze_backfill
    assert "::uuid" not in snooze_backfill
