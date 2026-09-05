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
    migration_0029 = next(
        migration for migration in migrations
        if migration.revision == "0029_ctssm_vnext_recovery_snapshot"
    )
    assert migration_0029.down_revision == "0028_workload_causal_provenance"
    migration_0030 = next(
        migration for migration in migrations
        if migration.revision == "0030_model_promotion_decisions"
    )
    assert migration_0030.down_revision == "0029_ctssm_vnext_recovery_snapshot"
    migration_0031 = next(
        migration for migration in migrations
        if migration.revision == "0031_parameter_learning_runs"
    )
    assert migration_0031.down_revision == "0030_model_promotion_decisions"
    migration_0032 = next(
        migration for migration in migrations
        if migration.revision == "0032_stage5_causal_hardening"
    )
    assert migration_0032.down_revision == "0031_parameter_learning_runs"
    migration_0033 = next(
        migration for migration in migrations
        if migration.revision == "0033_dataset_v6_profile_history"
    )
    assert migration_0033.down_revision == "0032_stage5_causal_hardening"
    migration_0034 = next(
        migration for migration in migrations
        if migration.revision == "0034_dataset_v7_active_history"
    )
    assert migration_0034.down_revision == "0033_dataset_v6_profile_history"
    migration_0035 = next(
        migration for migration in migrations
        if migration.revision == "0035_stage5_v7_runtime_cutover"
    )
    assert migration_0035.down_revision == "0034_dataset_v7_active_history"
    migration_0036 = next(
        migration for migration in migrations
        if migration.revision == "0036_stage5_effective_profile"
    )
    assert migration_0036.down_revision == "0035_stage5_v7_runtime_cutover"
    migration_0037 = next(
        migration for migration in migrations
        if migration.revision == "0037_stage6_care_jitai"
    )
    assert migration_0037.down_revision == "0036_stage5_effective_profile"


def test_course_schedule_import_migration_extends_stage6_head():
    migration_0038 = _migration(VERSIONS / "0038_course_schedule_import.py")
    assert migration_0038.down_revision == "0037_stage6_care_jitai"
    migration_0039 = _migration(
        VERSIONS / "0039_course_schedule_recurrence_strategy.py"
    )
    assert migration_0039.down_revision == "0038_course_schedule_import"


def test_0031_adds_auditable_parameter_learning_workflow(monkeypatch):
    migration = _migration(VERSIONS / "0031_parameter_learning_runs.py")
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

    assert tables[0][0] == "parameter_learning_runs"
    assert {
        "participant_id",
        "dataset_snapshot_id",
        "model_family",
        "parameters_before",
        "parameters_candidate",
        "training_metrics",
        "validation_metrics",
        "sample_count",
        "status",
        "created_at",
    } <= tables[0][1]
    assert len(indexes) == 2


def test_0032_separates_stage5_generation_and_adds_v5_exposures(monkeypatch):
    migration = _migration(VERSIONS / "0032_stage5_causal_hardening.py")
    columns = []
    checks = []
    indexes = []
    statements = []
    monkeypatch.setattr(migration.op, "drop_constraint", lambda *a, **k: None)
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: columns.append((table, column)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, table, fields, **kwargs: indexes.append(
            (name, table, tuple(fields), kwargs)
        ),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert [column.name for _, column in columns] == ["run_kind", "schedule_key"]
    assert "'care_intervention_exposure'" in checks[0][2]
    assert "'warning_delivery'" in checks[0][2]
    assert indexes[0][0] == "uq_parameter_learning_scheduled_week"
    assert indexes[0][3]["postgresql_where"] is not None
    assert any("hierarchical-ctssm-residual.v1" in value for value in statements)
    assert any("mindflow-ctssm-runtime-v9" in value for value in statements)


def test_0033_allows_frozen_participant_profile_history(monkeypatch):
    migration = _migration(VERSIONS / "0033_dataset_v6_profile_history.py")
    checks = []
    statements = []
    monkeypatch.setattr(migration.op, "drop_constraint", lambda *a, **k: None)
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()
    assert "'participant_profile'" in checks[-1][2]
    assert any("hierarchical-ctssm-residual.v2" in value for value in statements)
    assert any("mindflow-ctssm-runtime-v10" in value for value in statements)

    statements.clear()
    migration.downgrade()
    assert statements == [
        "DELETE FROM dataset_snapshot_items "
        "WHERE item_type = 'participant_profile'"
    ]
    assert "'participant_profile'" not in checks[-1][2]


def test_0034_freezes_active_history_and_revokes_old_promotions(monkeypatch):
    migration = _migration(VERSIONS / "0034_dataset_v7_active_history.py")
    checks = []
    statements = []
    monkeypatch.setattr(migration.op, "drop_constraint", lambda *a, **k: None)
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert "'learned_model_profile'" in checks[-1][2]
    assert any(
        "parameter_learning_runs" in value
        and "status = 'promoted'" in value
        and "mindflow-ctssm-runtime-v10" in value
        for value in statements
    )
    assert any(
        "learned_model_profiles" in value
        and "validation_status = 'rejected'" in value
        and "mindflow-ctssm-runtime-v10" in value
        for value in statements
    )

    statements.clear()
    migration.downgrade()
    assert statements == [
        "DELETE FROM dataset_snapshot_items "
        "WHERE item_type = 'learned_model_profile'"
    ]
    assert "'learned_model_profile'" not in checks[-1][2]


def test_0035_revokes_pre_v7_stage5_production_eligibility(monkeypatch):
    migration = _migration(VERSIONS / "0035_stage5_v7_runtime_cutover.py")
    statements = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert len(statements) == 2
    assert "parameter_learning_runs" in statements[0]
    assert "runs.status = 'promoted'" in statements[0]
    assert "schema_version <> 'mindflow-research-dataset-v7'" in statements[0]
    assert "learned_model_profiles" in statements[1]
    assert "mindflow-ctssm-runtime-v11" in statements[1]
    assert "stage5_promoted" in statements[1]
    assert "schema_version <> 'mindflow-research-dataset-v7'" in statements[1]


def test_0036_adds_durable_weekly_dataset_batch_identity(monkeypatch):
    migration = _migration(
        VERSIONS / "0036_stage5_effective_profile_batches.py"
    )
    columns = []
    checks = []
    indexes = []
    statements = []
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: columns.append((table, column)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)
    monkeypatch.setattr(migration.op, "alter_column", lambda *a, **k: None)
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_index",
        lambda name, table, fields, **kwargs: indexes.append(
            (name, table, tuple(fields), kwargs)
        ),
    )

    migration.upgrade()

    assert [column.name for _, column in columns] == [
        "purpose",
        "schedule_key",
    ]
    assert "purpose = 'manual_research'" in statements[0]
    assert checks == [
        (
            "ck_dataset_snapshot_batch_identity",
            "dataset_snapshots",
            "(purpose = 'stage5_weekly_calibration' AND schedule_key IS NOT NULL) "
            "OR (purpose <> 'stage5_weekly_calibration' AND schedule_key IS NULL)",
        )
    ]
    assert indexes[0][0] == "uq_dataset_snapshot_weekly_batch"
    assert indexes[0][3]["postgresql_where"] is not None


def test_0037_adds_jitai_outcome_and_mrt_ready_contracts(monkeypatch):
    migration = _migration(VERSIONS / "0037_stage6_care_jitai.py")
    columns = []
    tables = []
    checks = []
    statements = []
    alterations = []
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: columns.append((table, column.name)),
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
        "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )
    monkeypatch.setattr(migration.op, "create_index", lambda *a, **k: None)
    monkeypatch.setattr(migration.op, "execute", statements.append)
    monkeypatch.setattr(
        migration.op,
        "alter_column",
        lambda *args, **kwargs: alterations.append((args, kwargs)),
    )

    migration.upgrade()

    assert ("care_intervention_events", "vulnerability_score") in columns
    assert ("care_intervention_events", "receptivity_score") in columns
    assert ("care_intervention_events", "decision_score") in columns
    assert ("warning_schedules", "authorization_deadline") in columns
    assert {name for name, _ in tables} == {
        "care_intervention_outcomes",
        "intervention_randomization_events",
    }
    outcome_columns = dict(tables)["care_intervention_outcomes"]
    assert {
        "intervention_id",
        "baseline_state",
        "followup_30m",
        "followup_60m",
        "helpful_rating",
        "user_action",
        "context_json",
        "created_at",
    } <= outcome_columns
    assert "ck_care_outcome_helpful" in outcome_columns
    assert "ck_mrt_probability" in dict(tables)["intervention_randomization_events"]
    assert {name for name, _, _ in checks} == {
        "ck_care_interruption_tolerance",
        "ck_care_jitai_scores",
    }
    assert "SET authorization_deadline = risk_time" in statements[0]
    assert alterations[0] == (
        ("warning_schedules", "authorization_deadline"),
        {"nullable": False},
    )


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


def test_0029_expands_snapshot_evidence_types_and_has_safe_downgrade(monkeypatch):
    migration = _migration(
        VERSIONS / "0029_ctssm_vnext_recovery_snapshot.py"
    )
    dropped = []
    checks = []
    statements = []
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: dropped.append((args, kwargs)),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, table, condition: checks.append((name, table, condition)),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()
    assert all(
        item_type in checks[-1][2]
        for item_type in ("'psychometric'", "'daily_review'", "'slow_state'")
    )

    migration.downgrade()
    assert statements == [
        "DELETE FROM dataset_snapshot_items "
        "WHERE item_type IN ('psychometric', 'daily_review', 'slow_state')"
    ]
    assert "'psychometric'" not in checks[-1][2]
    assert len(dropped) == 2


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
