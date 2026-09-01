from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import uuid

import pytest
from sqlalchemy import select
from starlette.testclient import TestClient

from app.admin_web.main import create_app
from app.models import (
    CalendarSnapshot,
    DatasetSnapshot,
    DatasetSnapshotItem,
    EventSemanticCache,
    ForecastCurrentnessEvent,
    ForecastObservationMatch,
    ForecastSnapshot,
    LearnedModelProfile,
    Participant,
    ParticipantSlowState,
    StateObservation,
)
from app.repositories import ForecastSnapshotRepository
from app.services.research_evaluation import (
    DATASET_SCHEMA_V2,
    DATASET_SCHEMA_V3,
    DATASET_SCHEMA_V4,
    DATASET_SCHEMA_V5,
    EVALUATION_CODE_VERSION,
    ResearchEvaluationService,
)
from helpers import memory_database, participant
from test_admin_web import login, settings


LOCAL_DATE = date(2026, 8, 28)


def _seed_causal_forecast_and_observations(database, participant_id):
    repository = ForecastSnapshotRepository(database)
    saved = repository.save(
        participant_id,
        LOCAL_DATE,
        calendar_revision="calendar-stage2",
        semantic_revision="semantic-stage2",
        observation_revision="observation-stage2",
        algorithm_version="forecast.v4",
        forecast_version="forecast-stage2-v1",
        semantic_status="complete",
        semantic_input=[],
        curve=[
            {
                "time": "09:00",
                "stress_0_10": 5.0,
                "stress_interval_90_0_10": {"lower": 4.0, "upper": 6.0},
            },
            {
                "time": "09:05",
                "stress_0_10": 8.0,
                "stress_interval_90_0_10": {"lower": 7.0, "upper": 9.0},
            },
        ],
        peaks=[{"time": "09:05", "stress_0_10": 8.0}],
        warning_windows=[],
        output={
            "model_family": "stress-ctssm.m1",
            "model_variant": "m1",
            "model_spec_version": "stress-ctssm-model-spec.v1:m1",
            "promotion_decision_id": "promotion-stage2",
            "promotion_parameters_hash": "parameters-stage2",
            "initial_state": {
                "stress_0_10": 4.0,
                "vitality_0_10": 7.0,
            },
            "initial_state_revision": "initial-state-stage2",
            "classified_calendar_events": [
                {
                    "id": "class-1",
                    "start_time": "2026-08-28T08:30:00+08:00",
                    "end_time": "2026-08-28T09:30:00+08:00",
                    "event_type": "class",
                    "course_name": "高等数学",
                }
            ]
        },
    )
    activation = datetime(2026, 8, 28, 0, 0, tzinfo=timezone.utc)
    with database.session() as session:
        forecast = session.get(ForecastSnapshot, uuid.UUID(saved["id"]))
        forecast.generated_at = activation - timedelta(hours=1)
        event = session.execute(
            select(ForecastCurrentnessEvent).where(
                ForecastCurrentnessEvent.forecast_id == forecast.id
            )
        ).scalar_one()
        event.occurred_at = activation
        session.add_all(
            [
                StateObservation(
                    participant_id=participant_id,
                    observation_type="checkin",
                    source_message_id="stage2-observation-1",
                    payload_json={
                        "stress_0_10": 7.0,
                        "current_workload_0_10": 8.0,
                    },
                    observed_at=datetime(2026, 8, 28, 1, 2, tzinfo=timezone.utc),
                    created_at=datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc),
                ),
                StateObservation(
                    participant_id=participant_id,
                    observation_type="checkin",
                    source_message_id="stage2-observation-2",
                    payload_json={
                        "stress_0_10": 9.0,
                        "current_workload_0_10": 10.0,
                    },
                    observed_at=datetime(2026, 8, 28, 1, 6, tzinfo=timezone.utc),
                    created_at=datetime(2026, 8, 28, 2, 0, tzinfo=timezone.utc),
                ),
            ]
        )
    return saved


def _convert_snapshot_to_legacy_v2(database, service, snapshot_id):
    with database.session() as session:
        snapshot = session.get(DatasetSnapshot, snapshot_id)
        participant_items = session.execute(
            select(DatasetSnapshotItem).where(
                DatasetSnapshotItem.dataset_snapshot_id == snapshot_id,
                DatasetSnapshotItem.item_type == "participant",
            )
        ).scalars().all()
        for item in participant_items:
            session.delete(item)
        session.flush()
        rows = session.execute(
            select(DatasetSnapshotItem).where(
                DatasetSnapshotItem.dataset_snapshot_id == snapshot_id
            )
        ).scalars().all()
        items = [
            {
                "item_type": row.item_type,
                "source_id": row.source_id,
                "source_version": row.source_version,
                "participant_id": row.participant_id,
                "local_date": row.local_date,
                "source_hash": row.source_hash,
                "metadata": dict(row.metadata_json),
            }
            for row in rows
        ]
        snapshot.schema_version = DATASET_SCHEMA_V2
        snapshot_view = service._snapshot_view(snapshot)
        contract = {
            "schema_version": DATASET_SCHEMA_V2,
            "date_start": snapshot_view["date_start"],
            "date_end": snapshot_view["date_end"],
            "participant_filter": snapshot_view["participant_filter"],
            "observation_cutoff": snapshot_view["observation_cutoff"],
            "calendar_cutoff": snapshot_view["calendar_cutoff"],
        }
        type_count = lambda kind: sum(
            item["item_type"] == kind for item in items
        )
        snapshot.manifest_json = {
            "schema_version": DATASET_SCHEMA_V2,
            "participant_count": 1,
            "observation_count": type_count("observation"),
            "forecast_count": type_count("forecast"),
            "calendar_count": type_count("calendar"),
            "item_count": len(items),
            "manifest_hash": service._manifest_hash(contract, items),
        }


def _convert_snapshot_to_historical_v3(database, service, snapshot_id):
    with database.session() as session:
        snapshot = session.get(DatasetSnapshot, snapshot_id)
        rows = session.execute(
            select(DatasetSnapshotItem).where(
                DatasetSnapshotItem.dataset_snapshot_id == snapshot_id
            )
        ).scalars().all()
        items = [
            {
                "item_type": row.item_type,
                "source_id": row.source_id,
                "source_version": row.source_version,
                "participant_id": row.participant_id,
                "local_date": row.local_date,
                "source_hash": row.source_hash,
                "metadata": dict(row.metadata_json),
            }
            for row in rows
        ]
        assert not {
            "psychometric", "daily_review", "slow_state"
        } & {item["item_type"] for item in items}
        snapshot.schema_version = DATASET_SCHEMA_V3
        view = service._snapshot_view(snapshot)
        contract = {
            "schema_version": DATASET_SCHEMA_V3,
            "date_start": view["date_start"],
            "date_end": view["date_end"],
            "participant_filter": view["participant_filter"],
            "observation_cutoff": view["observation_cutoff"],
            "calendar_cutoff": view["calendar_cutoff"],
        }
        count = lambda kind: sum(item["item_type"] == kind for item in items)
        snapshot.manifest_json = {
            "schema_version": DATASET_SCHEMA_V3,
            "participant_count": count("participant"),
            "observation_count": count("observation"),
            "forecast_count": count("forecast"),
            "calendar_count": count("calendar"),
            "item_count": len(items),
            "manifest_hash": service._manifest_hash(contract, items),
        }


def test_historical_v3_snapshot_remains_immutable_and_evaluable_after_stage4():
    database = memory_database()
    person = participant(database, "HISTORICAL-V3")
    _seed_causal_forecast_and_observations(database, person.id)
    service = ResearchEvaluationService(database, "Asia/Shanghai")
    snapshot = service.create_dataset_snapshot(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
    )
    snapshot_id = uuid.UUID(snapshot["id"])
    _convert_snapshot_to_historical_v3(database, service, snapshot_id)
    before = service.snapshot_items(snapshot_id)

    cohort = service.create_evaluation_run(snapshot_id, "forecast.v4")
    individual = service.create_evaluation_run(
        snapshot_id, "forecast.v4", participant_id=person.id
    )

    assert cohort["status"] == individual["status"] == "completed"
    assert service.list_snapshots()[0]["schema_version"] == DATASET_SCHEMA_V3
    assert service.snapshot_items(snapshot_id) == before
    manifest = service.list_snapshots()[0]["manifest"]
    assert "psychometric_count" not in manifest
    assert "daily_review_count" not in manifest
    assert "slow_state_count" not in manifest

    with database.session() as session:
        item = session.execute(
            select(DatasetSnapshotItem).where(
                DatasetSnapshotItem.dataset_snapshot_id == snapshot_id,
                DatasetSnapshotItem.item_type == "observation",
            )
        ).scalars().first()
        item.source_hash = "0" * 64
    with pytest.raises(ValueError, match="dataset snapshot manifest mismatch"):
        service.create_evaluation_run(snapshot_id, "forecast.v4")


def test_stage2_materializes_causal_grid_matches_and_exact_metrics():
    database = memory_database()
    person = participant(database, "STAGE2-MATCH")
    _seed_causal_forecast_and_observations(database, person.id)
    service = ResearchEvaluationService(database, "Asia/Shanghai")

    rebuilt = service.rebuild_matches(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
    )
    evaluation = service.evaluation(LOCAL_DATE, LOCAL_DATE, person.id)

    assert rebuilt["created"] == 2
    assert rebuilt["updated"] == rebuilt["unmatched"] == 0
    assert rebuilt["examined"] == 2
    assert rebuilt["match_schema_version"] == "forecast-observation-grid.v2"
    assert len(evaluation["matches"]) == 2
    first = evaluation["matches"][0]
    assert first["forecast_version"] == "forecast-stage2-v1"
    assert first["predicted_stress"] == 5.0
    assert first["actual_stress"] == 7.0
    assert first["residual"] == 2.0
    assert first["context"]["event_types"] == ["class"]
    assert first["context"]["courses"] == ["高等数学"]
    assert first["context"]["model_variant"] == "m1"
    assert first["context"]["promotion_decision_id"] == "promotion-stage2"
    assert first["context"]["promotion_parameters_hash"] == "parameters-stage2"
    metrics = evaluation["metrics"]
    assert metrics["sample_count"] == 2
    assert metrics["mae"] == 1.5
    assert metrics["rmse"] == round(math.sqrt(2.5), 4)
    assert metrics["median_absolute_error"] == 1.5
    assert metrics["interval_nominal_coverage"] == 0.9
    assert metrics["interval_90_coverage"] == 0.5
    assert metrics["mean_interval_width"] == 2.0
    assert metrics["observed_peak_proxy_magnitude_error"] == 1.0
    assert metrics["observed_peak_proxy_timing_error_minutes"] == 1.0
    assert metrics["peak_proxy_day_count"] == 1
    assert metrics["peak_proxy_mean_samples_per_day"] == 2.0
    diagnostics = evaluation["residual_diagnostics"]
    assert diagnostics["event_type"][0]["group"] == "class"
    assert diagnostics["course"][0]["group"] == "高等数学"
    assert diagnostics["workload"][-1]["group"] == "8–10"

    # Idempotent rebuild updates, rather than duplicating, the materialized rows.
    second = service.rebuild_matches(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
    )
    assert second["created"] == 0
    assert second["updated"] == 2
    with database.session() as session:
        assert len(session.execute(select(ForecastObservationMatch)).scalars().all()) == 2


def test_dataset_snapshot_and_model_run_are_bound_to_cutoffs_and_model_version():
    database = memory_database()
    person = participant(database, "STAGE2-SNAPSHOT")
    _seed_causal_forecast_and_observations(database, person.id)
    service = ResearchEvaluationService(database, "Asia/Shanghai")

    snapshot = service.create_dataset_snapshot(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc),
    )
    run = service.create_evaluation_run(
        uuid.UUID(snapshot["id"]),
        "forecast.v4",
    )

    assert snapshot["schema_version"] == DATASET_SCHEMA_V5
    assert snapshot["manifest"]["participant_count"] == 1
    assert snapshot["manifest"]["observation_count"] == 2
    assert snapshot["manifest"]["forecast_count"] == 1
    assert snapshot["manifest"]["calendar_count"] == 1
    assert len(snapshot["manifest"]["manifest_hash"]) == 64
    frozen = service.snapshot_items(uuid.UUID(snapshot["id"]))
    assert {item["item_type"] for item in frozen} == {
        "participant",
        "observation",
        "forecast",
        "forecast_currentness",
        "calendar",
        "match_source",
    }
    frozen_forecast = next(
        item for item in frozen if item["item_type"] == "forecast"
    )
    assert frozen_forecast["metadata"]["model_variant"] == "m1"
    assert frozen_forecast["metadata"]["initial_state_revision"] == (
        "initial-state-stage2"
    )
    assert run["dataset_snapshot_id"] == snapshot["id"]
    assert run["model_version"] == "forecast.v4"
    assert run["evaluation_mode"] == "historical_online"
    assert run["status"] == "completed"
    assert run["metrics"]["matched_observation_count"] == 2
    assert run["evaluation_code_version"] == EVALUATION_CODE_VERSION
    assert run["metrics"]["config"]["evaluation_code_version"] == (
        EVALUATION_CODE_VERSION
    )
    assert run["metrics"]["config"]["manifest_hash"] == snapshot["manifest"][
        "manifest_hash"
    ]
    assert service.list_snapshots()[0]["id"] == snapshot["id"]
    assert service.list_runs()[0]["id"] == run["id"]


def test_historical_online_filters_exact_promotion_identity_without_mixing_revisions():
    database = memory_database()
    person = participant(database, "IDENTITY-FILTER")
    service = ResearchEvaluationService(database, "Asia/Shanghai")
    snapshot_id = uuid.uuid4()
    cutoff = datetime(2030, 3, 2, tzinfo=timezone.utc)
    local_day = date(2030, 3, 1)
    identities = [
        ("decision-1", "hash-1", 5.0, 6.0),
        ("decision-1", "hash-1", 6.0, 7.0),
        ("decision-2", "hash-2", 8.0, 7.0),
    ]
    items = [
        service._item(
            "match_source",
            f"identity-observation-{index}",
            "forecast-observation-grid.v2",
            person.id,
            local_day,
            {
                "participant_id": str(person.id),
                "local_date": local_day.isoformat(),
                "forecast_id": f"identity-forecast-{index}",
                "forecast_version": f"identity-v{index}",
                "observation_id": f"identity-observation-{index}",
                "observed_at": (
                    f"2030-03-01T{index + 8:02d}:00:00+00:00"
                ),
                "predicted_stress": predicted,
                "actual_stress": actual,
                "residual": actual - predicted,
                "prediction_lower": 0.0,
                "prediction_upper": 10.0,
                "context": {
                    "algorithm_version": "mindflow-ctssm-runtime-v8",
                    "engine_version": "mindflow-ctssm-runtime-v8",
                    "model_family": "stress-ctssm.m1",
                    "model_variant": "m1",
                    "model_spec_version": "stress-ctssm-model-spec.v1:m1",
                    "promotion_decision_id": decision,
                    "promotion_parameters_hash": parameters_hash,
                },
            },
        )
        for index, (decision, parameters_hash, predicted, actual) in enumerate(
            identities
        )
    ]
    contract = {
        "schema_version": DATASET_SCHEMA_V2,
        "date_start": local_day.isoformat(),
        "date_end": local_day.isoformat(),
        "participant_filter": {},
        "observation_cutoff": cutoff.isoformat(),
        "calendar_cutoff": cutoff.isoformat(),
    }
    manifest = {
        "schema_version": DATASET_SCHEMA_V2,
        "item_count": len(items),
        "observation_count": 0,
        "forecast_count": 0,
        "calendar_count": 0,
        "manifest_hash": service._manifest_hash(contract, items),
    }
    with database.session() as session:
        session.add(
            DatasetSnapshot(
                id=snapshot_id,
                date_start=local_day,
                date_end=local_day,
                participant_filter={},
                observation_cutoff=cutoff,
                calendar_cutoff=cutoff,
                schema_version=DATASET_SCHEMA_V2,
                manifest_json=manifest,
            )
        )
        session.add_all(
            [
                DatasetSnapshotItem(
                    dataset_snapshot_id=snapshot_id,
                    item_type=item["item_type"],
                    source_id=item["source_id"],
                    source_version=item["source_version"],
                    participant_id=item["participant_id"],
                    local_date=item["local_date"],
                    source_hash=item["source_hash"],
                    metadata_json=item["metadata"],
                )
                for item in items
            ]
        )

    decision_run = service.create_evaluation_run(
        snapshot_id,
        "m1-decision-1",
        evaluation_mode="historical_online",
        model_identity_filter={"promotion_decision_id": "decision-1"},
    )
    hash_run = service.create_evaluation_run(
        snapshot_id,
        "m1-hash-2",
        evaluation_mode="historical_online",
        model_identity_filter={"promotion_parameters_hash": "hash-2"},
    )
    mismatched_pair = service.create_evaluation_run(
        snapshot_id,
        "m1-mismatched-revision",
        evaluation_mode="historical_online",
        model_identity_filter={
            "promotion_decision_id": "decision-1",
            "promotion_parameters_hash": "hash-2",
        },
    )

    assert decision_run["metrics"]["matched_observation_count"] == 2
    assert decision_run["metrics"]["config"][
        "matched_promotion_decision_ids"
    ] == ["decision-1"]
    assert decision_run["metrics"]["config"]["matched_parameters_hashes"] == [
        "hash-1"
    ]
    assert hash_run["metrics"]["matched_observation_count"] == 1
    assert hash_run["metrics"]["config"]["model_identity_filter"] == {
        "promotion_parameters_hash": "hash-2"
    }
    assert decision_run["metrics"]["config"]["snapshot_source_set"][
        "promotion_decision_ids"
    ] == ["decision-1", "decision-2"]
    assert decision_run["metrics"]["config"]["evaluation_source_set"][
        "promotion_decision_ids"
    ] == ["decision-1"]
    assert decision_run["metrics"]["config"]["evaluation_source_set"][
        "promotion_parameters_hashes"
    ] == ["hash-1"]
    assert len(
        decision_run["metrics"]["config"]["evaluation_source_set"][
            "match_source_hashes"
        ]
    ) == 2
    assert mismatched_pair["metrics"]["matched_observation_count"] == 0
    assert mismatched_pair["metrics"]["config"]["evaluation_source_set"][
        "observation_ids"
    ] == []


def test_longitudinal_parameter_history_and_data_quality_cover_stage2_gates():
    database = memory_database()
    person = participant(database, "synthetic-stage2")
    with database.session() as session:
        session.get(Participant, person.id).created_at = datetime(
            2026, 8, 14, tzinfo=timezone.utc
        )
        session.add_all(
            [
                LearnedModelProfile(
                    participant_id=person.id,
                    version=1,
                    parameters_json={
                        "S_star_init": 52.0,
                        "stress_reactivity": 1.1,
                        "recovery_rate": 0.8,
                        "workload_gain": 1.4,
                    },
                    uncertainty_json={"stress_reactivity": {"std_error": 0.2}},
                    source="calibration.v2",
                    model_version="forecast.v4",
                    validation_status="validated",
                    sample_count=28,
                    day_count=14,
                    confidence=0.8,
                    window_start=date(2026, 8, 15),
                    window_end=LOCAL_DATE,
                ),
                ParticipantSlowState(
                    participant_id=person.id,
                    effective_at=datetime(2026, 8, 28, 0, tzinfo=timezone.utc),
                    cadence="daily",
                    rolling_7d_stress=5.0,
                    rolling_7d_workload=7.0,
                    rolling_7d_energy=4.0,
                    recent_recovery_quality=5.0,
                    recent_sleep_debt=2.0,
                    exam_period_flag=False,
                    source="derived.v1",
                ),
                StateObservation(
                    participant_id=person.id,
                    observation_type="checkin",
                    source_message_id="late-backfill",
                    payload_json={"stress_0_10": 6.0},
                    observed_at=datetime(2026, 8, 28, 1, tzinfo=timezone.utc),
                    created_at=datetime(2026, 8, 28, 8, tzinfo=timezone.utc),
                ),
                StateObservation(
                    participant_id=person.id,
                    observation_type="checkin",
                    source_message_id="time-anomaly",
                    payload_json={"stress_0_10": 5.0},
                    observed_at=datetime(2026, 8, 28, 10, tzinfo=timezone.utc),
                    created_at=datetime(2026, 8, 28, 9, tzinfo=timezone.utc),
                ),
                CalendarSnapshot(
                    participant_id=person.id,
                    local_date=LOCAL_DATE,
                    calendar_revision="degraded-duplicate",
                    events_json=[{"id": "duplicate"}, {"id": "duplicate"}],
                    snapshot_state="provider_degraded",
                    degraded=True,
                    updated_at=datetime(2026, 8, 28, 1, tzinfo=timezone.utc),
                ),
                EventSemanticCache(
                    participant_id=person.id,
                    fingerprint="partial-event",
                    schema_version="v1",
                    prompt_version="v1",
                    model="semantic-v1",
                    assessment_json={},
                    status="partial",
                    created_at=datetime(2026, 8, 28, 2, tzinfo=timezone.utc),
                ),
                EventSemanticCache(
                    participant_id=person.id,
                    fingerprint="rejected-event",
                    schema_version="v1",
                    prompt_version="v1",
                    model="semantic-v1",
                    assessment_json={},
                    status="rejected",
                    created_at=datetime(2026, 8, 28, 2, tzinfo=timezone.utc),
                ),
            ]
        )
    service = ResearchEvaluationService(database, "Asia/Shanghai")

    longitudinal = service.participant_longitudinal(person.id, LOCAL_DATE, 14)
    quality = service.data_quality(
        LOCAL_DATE,
        LOCAL_DATE + timedelta(days=1),
        person.id,
    )

    history = longitudinal["parameter_history"][0]
    assert history["parameters"]["S_star"]["estimate"] == 52.0
    assert history["parameters"]["reactivity"]["uncertainty"] == {
        "std_error": 0.2
    }
    assert longitudinal["workload_trend_7d"][0]["rolling_7d_workload"] == 7.0
    assert {
        "late_ema",
        "backfilled_observation",
        "daily_review_missing",
        "calendar_degraded",
        "semantic_partial",
        "semantic_rejected",
        "duplicate_event",
        "synthetic_row",
        "time_anomaly",
    } <= set(quality["counts"])
    assert "missing_ema" not in quality["counts"]


def test_user_initiated_checkins_use_observed_day_rate_and_join_date_exposure():
    database = memory_database()
    first = participant(database, "EXPOSURE-A")
    second = participant(database, "EXPOSURE-B")
    with database.session() as session:
        session.get(Participant, first.id).created_at = datetime(
            2026, 7, 31, 16, tzinfo=timezone.utc
        )
        session.get(Participant, second.id).created_at = datetime(
            2026, 8, 19, 16, tzinfo=timezone.utc
        )
    service = ResearchEvaluationService(database, "Asia/Shanghai")

    dashboard = service.cohort_dashboard(
        date(2026, 8, 1), date(2026, 8, 28)
    )
    quality = service.data_quality(date(2026, 8, 1), date(2026, 8, 28))

    completeness = dashboard["data_completeness"]
    assert completeness["eligible_participant_days"] == 28 + 9
    assert completeness["ema_observed_day_rate"] == 0.0
    assert "ema_adherence" not in completeness
    assert "missing_ema" not in quality["counts"]
    second_dates = {
        item["local_date"]
        for item in quality["items"]
        if item["participant_id"] == str(second.id)
    }
    assert min(second_dates) == "2026-08-20"

    longitudinal = service.participant_longitudinal(
        second.id, date(2026, 8, 28), 14
    )
    assert longitudinal["eligible_day_count_14d"] == 9
    assert longitudinal["ema_observed_day_rate_14d"] == 0.0
    assert "ema_adherence_14d" not in longitudinal


def test_snapshot_and_historical_evaluation_ignore_later_live_database_changes():
    database = memory_database()
    person = participant(database, "STAGE2-IMMUTABLE")
    _seed_causal_forecast_and_observations(database, person.id)
    with database.session() as session:
        session.add(
            CalendarSnapshot(
                participant_id=person.id,
                local_date=LOCAL_DATE,
                calendar_revision="calendar-stage2",
                events_json=[{"id": "original", "summary": "原始日程"}],
                snapshot_state="current",
                degraded=False,
                updated_at=datetime(2026, 8, 28, 0, tzinfo=timezone.utc),
            )
        )
    service = ResearchEvaluationService(database, "Asia/Shanghai")
    snapshot = service.create_dataset_snapshot(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
    )
    snapshot_id = uuid.UUID(snapshot["id"])
    before_items = service.snapshot_items(snapshot_id)
    first_run = service.create_evaluation_run(
        snapshot_id, "forecast.v4", evaluation_mode="historical_online"
    )

    with database.session() as session:
        session.add(
            StateObservation(
                participant_id=person.id,
                observation_type="checkin",
                source_message_id="after-snapshot",
                payload_json={"stress_0_10": 10.0},
                observed_at=datetime(2026, 8, 28, 1, 10, tzinfo=timezone.utc),
                created_at=datetime(2026, 8, 28, 4, tzinfo=timezone.utc),
            )
        )
        calendar = session.execute(
            select(CalendarSnapshot).where(
                CalendarSnapshot.participant_id == person.id,
                CalendarSnapshot.local_date == LOCAL_DATE,
            )
        ).scalar_one()
        calendar.calendar_revision = "calendar-live-mutated"
        calendar.events_json = [{"id": "later", "summary": "后续日程"}]
        calendar.updated_at = datetime(2026, 8, 28, 5, tzinfo=timezone.utc)
    ForecastSnapshotRepository(database).save(
        person.id,
        LOCAL_DATE,
        calendar_revision="calendar-live-mutated",
        semantic_revision="semantic-later",
        observation_revision="observation-later",
        algorithm_version="forecast.v5",
        forecast_version="forecast-stage2-v2",
        semantic_status="complete",
        semantic_input=[],
        curve=[{"time": "09:00", "stress_0_10": 10.0}],
        peaks=[{"time": "09:00", "stress_0_10": 10.0}],
        warning_windows=[],
        output={"classified_calendar_events": []},
    )

    second_run = service.create_evaluation_run(
        snapshot_id, "forecast.v4", evaluation_mode="historical_online"
    )
    after_items = service.snapshot_items(snapshot_id)
    persisted_snapshot = service.list_snapshots()[0]

    item_identity = lambda rows: [
        (row["item_type"], row["source_id"], row["source_version"], row["source_hash"])
        for row in rows
    ]
    assert item_identity(after_items) == item_identity(before_items)
    assert persisted_snapshot["manifest"]["manifest_hash"] == snapshot["manifest"][
        "manifest_hash"
    ]
    assert first_run["metrics"] == second_run["metrics"]
    assert first_run["metrics"]["config"]["snapshot_source_set"] == second_run[
        "metrics"
    ]["config"]["snapshot_source_set"]
    assert first_run["metrics"]["config"]["evaluation_source_set"] == second_run[
        "metrics"
    ]["config"]["evaluation_source_set"]

    offline = service.create_evaluation_run(
        snapshot_id, "candidate.v1", evaluation_mode="offline_replay"
    )
    assert offline["evaluation_mode"] == "offline_replay"
    assert offline["status"] == "completed"
    assert offline["metrics"]["config"]["dataset_schema_version"] == (
            DATASET_SCHEMA_V5
    )


def test_peak_proxy_is_participant_isolated_and_requires_two_samples():
    def match(participant_id, actual, observed, predicted_peak, predicted_time):
        return {
            "participant_id": participant_id,
            "local_date": "2026-08-28",
            "forecast_version": "shared-version",
            "actual_stress": actual,
            "residual": 1.0,
            "prediction_lower": None,
            "prediction_upper": None,
            "context": {
                "time_of_day": observed,
                "forecast_peak_stress": predicted_peak,
                "forecast_peak_time": predicted_time,
            },
        }

    matches = [
        match("A", 4.0, "09:00", 6.0, "09:10"),
        match("A", 5.0, "09:05", 6.0, "09:10"),
        match("B", 8.0, "09:00", 7.0, "09:15"),
        match("B", 9.0, "09:05", 7.0, "09:15"),
        match("C", 10.0, "09:00", 1.0, "12:00"),
    ]

    metrics = ResearchEvaluationService.metrics(matches)

    assert metrics["peak_proxy_day_count"] == 2
    assert metrics["peak_proxy_mean_samples_per_day"] == 2.0
    assert metrics["observed_peak_proxy_magnitude_error"] == 1.5
    assert metrics["observed_peak_proxy_timing_error_minutes"] == 7.5
    assert "peak_magnitude_error" not in metrics


def test_non_momentary_observation_is_isolated_from_all_stage2_metrics():
    database = memory_database()
    person = participant(database, "STAGE2-TYPE-ISOLATION")
    _seed_causal_forecast_and_observations(database, person.id)
    with database.session() as session:
        session.get(Participant, person.id).created_at = datetime(
            2026, 8, 27, 0, tzinfo=timezone.utc
        )
        debug = StateObservation(
            participant_id=person.id,
            observation_type="research_debug",
            source_message_id="stage2-non-momentary",
            payload_json={"stress_0_10": 10.0},
            observed_at=datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc),
        )
        session.add(debug)
        session.flush()
        debug_id = debug.id
    service = ResearchEvaluationService(database, "Asia/Shanghai")

    rebuilt = service.rebuild_matches(date_start=LOCAL_DATE, date_end=LOCAL_DATE)
    evaluation = service.evaluation(LOCAL_DATE, LOCAL_DATE, person.id)
    dashboard = service.cohort_dashboard(LOCAL_DATE, LOCAL_DATE)
    longitudinal = service.participant_longitudinal(person.id, LOCAL_DATE, 14)
    snapshot = service.create_dataset_snapshot(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
    )
    observation_items = service.snapshot_items(
        uuid.UUID(snapshot["id"]), "observation"
    )

    assert rebuilt["examined"] == 2
    assert rebuilt["created"] == 2
    with database.session() as session:
        assert session.execute(
            select(ForecastObservationMatch).where(
                ForecastObservationMatch.observation_id == debug_id
            )
        ).scalar_one_or_none() is None
    observation_ids = {item["source_id"] for item in observation_items}
    assert str(debug_id) not in observation_ids
    completeness = dashboard["data_completeness"]
    assert completeness["ema_count"] == 2
    assert completeness["ema_observed_day_rate"] == 1.0
    assert longitudinal["stress_trend_14d"] == [
        {"date": LOCAL_DATE.isoformat(), "mean_stress": 8.0, "ema_count": 2}
    ]
    assert evaluation["metrics"]["sample_count"] == 2
    assert evaluation["metrics"]["mae"] == 1.5
    assert evaluation["metrics"]["rmse"] == round(math.sqrt(2.5), 4)


def test_snapshot_membership_includes_zero_sample_participant_and_changes_hash():
    database = memory_database()
    first = participant(database, "STAGE2-COHORT-P001")
    _seed_causal_forecast_and_observations(database, first.id)
    service = ResearchEvaluationService(database, "Asia/Shanghai")
    observation_cutoff = datetime(2026, 8, 28, 3, tzinfo=timezone.utc)

    first_only = service.create_dataset_snapshot(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
        observation_cutoff=observation_cutoff,
        calendar_cutoff=observation_cutoff,
    )
    second = participant(database, "STAGE2-COHORT-P002")
    with database.session() as session:
        session.get(Participant, second.id).created_at = datetime(
            2026, 8, 28, 2, 30, tzinfo=timezone.utc
        )
    both = service.create_dataset_snapshot(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
        observation_cutoff=observation_cutoff,
        calendar_cutoff=observation_cutoff,
    )

    membership = service.snapshot_items(uuid.UUID(both["id"]), "participant")
    assert both["manifest"]["participant_count"] == 2
    assert len(membership) == 2
    assert {item["participant_id"] for item in membership} == {
        str(first.id), str(second.id)
    }
    second_membership = next(
        item for item in membership if item["participant_id"] == str(second.id)
    )
    assert second_membership["source_version"] == "participant-membership.v1"
    assert second_membership["metadata"]["participant_code"] == second.participant_code
    assert second_membership["metadata"]["status_at_snapshot"] == "active"
    assert first_only["manifest"]["participant_count"] == 1
    assert both["manifest"]["manifest_hash"] != first_only["manifest"][
        "manifest_hash"
    ]
    match_sources = service.snapshot_items(uuid.UUID(both["id"]), "match_source")
    assert all(
        item["participant_id"] != str(second.id)
        for item in match_sources
    )

    zero_sample = service.create_evaluation_run(
        uuid.UUID(both["id"]), "forecast.v4", participant_id=second.id
    )
    assert zero_sample["status"] == "completed"
    assert zero_sample["metrics"]["metrics"]["sample_count"] == 0
    assert zero_sample["metrics"]["matched_observation_count"] == 0

    with database.session() as session:
        item = session.execute(
            select(DatasetSnapshotItem).where(
                DatasetSnapshotItem.dataset_snapshot_id == uuid.UUID(both["id"]),
                DatasetSnapshotItem.item_type == "participant",
                DatasetSnapshotItem.participant_id == second.id,
            )
        ).scalar_one()
        metadata = dict(item.metadata_json)
        metadata["participant_code"] = "TAMPERED"
        item.metadata_json = metadata
    with pytest.raises(ValueError, match="dataset snapshot manifest mismatch"):
        service.create_evaluation_run(
            uuid.UUID(both["id"]), "forecast.v4", participant_id=second.id
        )

    first_only_id = uuid.UUID(first_only["id"])
    with database.session() as session:
        membership_item = session.execute(
            select(DatasetSnapshotItem).where(
                DatasetSnapshotItem.dataset_snapshot_id == first_only_id,
                DatasetSnapshotItem.item_type == "participant",
            )
        ).scalar_one()
        session.delete(membership_item)
    with pytest.raises(
        ValueError, match="dataset snapshot manifest/items count mismatch"
    ):
        service.create_evaluation_run(first_only_id, "forecast.v4")


def test_legacy_v2_snapshot_remains_evaluable_without_membership_items():
    database = memory_database()
    person = participant(database, "STAGE2-LEGACY-V2")
    _seed_causal_forecast_and_observations(database, person.id)
    service = ResearchEvaluationService(database, "Asia/Shanghai")
    snapshot = service.create_dataset_snapshot(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
    )
    snapshot_id = uuid.UUID(snapshot["id"])
    _convert_snapshot_to_legacy_v2(database, service, snapshot_id)

    legacy_items = service.snapshot_items(snapshot_id)
    assert {item["item_type"] for item in legacy_items} == {
        "observation",
        "forecast",
        "forecast_currentness",
        "calendar",
        "match_source",
    }
    cohort_run = service.create_evaluation_run(snapshot_id, "forecast.v4")
    participant_run = service.create_evaluation_run(
        snapshot_id, "forecast.v4", participant_id=person.id
    )
    assert cohort_run["status"] == "completed"
    assert cohort_run["metrics"]["metrics"]["sample_count"] == 2
    assert cohort_run["metrics"]["config"]["dataset_schema_version"] == (
        DATASET_SCHEMA_V2
    )
    assert participant_run["metrics"]["matched_observation_count"] == 2

    unknown = participant(database, "STAGE2-LEGACY-UNKNOWN")
    with pytest.raises(ValueError, match="legacy_v2_snapshot_membership_unknown"):
        service.create_evaluation_run(
            snapshot_id, "forecast.v4", participant_id=unknown.id
        )

    with database.session() as session:
        item = session.execute(
            select(DatasetSnapshotItem).where(
                DatasetSnapshotItem.dataset_snapshot_id == snapshot_id,
                DatasetSnapshotItem.item_type == "observation",
            )
        ).scalars().first()
        metadata = dict(item.metadata_json)
        metadata["payload"] = {"stress_0_10": 10.0}
        item.metadata_json = metadata
    with pytest.raises(ValueError, match="dataset snapshot manifest mismatch"):
        service.create_evaluation_run(snapshot_id, "forecast.v4")


def test_snapshot_filter_fails_closed_and_calendar_cutoff_is_effective():
    database = memory_database()
    person = participant(database, "STAGE2-FILTER")
    _seed_causal_forecast_and_observations(database, person.id)
    service = ResearchEvaluationService(database, "Asia/Shanghai")

    with pytest.raises(ValueError, match="unknown participant_codes: UNKNOWN"):
        service.create_dataset_snapshot(
            date_start=LOCAL_DATE,
            date_end=LOCAL_DATE,
            participant_filter={
                "participant_codes": [person.participant_code, "UNKNOWN"]
            },
        )

    snapshot = service.create_dataset_snapshot(
        date_start=LOCAL_DATE,
        date_end=LOCAL_DATE,
        participant_filter={"participant_codes": [person.participant_code]},
        observation_cutoff=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
        calendar_cutoff=datetime(2026, 8, 27, 22, tzinfo=timezone.utc),
    )
    items = service.snapshot_items(uuid.UUID(snapshot["id"]))
    assert {item["item_type"] for item in items} == {
        "participant", "observation"
    }
    assert snapshot["manifest"]["forecast_count"] == 0
    assert snapshot["manifest"]["calendar_count"] == 0


def test_stage2_admin_routes_and_research_ui_are_exposed_with_auth_and_csrf():
    database = memory_database()
    person = participant(database, "STAGE2-ADMIN")
    _seed_causal_forecast_and_observations(database, person.id)
    browser = TestClient(create_app(database, settings()))

    assert browser.get("/admin/api/research/dashboard").status_code == 401
    assert browser.get("/admin/data-quality").status_code == 200
    assert browser.get("/admin/research").status_code == 200
    auth = login(browser)
    query = f"date_start={LOCAL_DATE}&date_end={LOCAL_DATE}"
    assert browser.get(f"/admin/api/research/dashboard?{query}").status_code == 200
    assert browser.get(f"/admin/api/data-quality?{query}").status_code == 200
    assert browser.get(
        f"/admin/api/participants/{person.participant_code}/longitudinal?through={LOCAL_DATE}&days=14"
    ).status_code == 200
    assert browser.get(
        f"/admin/api/participants/{person.participant_code}/evaluation?{query}"
    ).status_code == 200
    assert browser.post(f"/admin/api/research/matches/rebuild?{query}").status_code == 401
    rebuilt = browser.post(
        f"/admin/api/research/matches/rebuild?{query}",
        headers={"X-CSRF-Token": auth["csrf_token"]},
        json={},
    )
    assert rebuilt.status_code == 200
    assert rebuilt.json()["created"] == 2
    snapshot = browser.post(
        "/admin/api/research/dataset-snapshots",
        headers={"X-CSRF-Token": auth["csrf_token"]},
        json={
            "date_start": LOCAL_DATE.isoformat(),
            "date_end": LOCAL_DATE.isoformat(),
            "participant_filter": {
                "participant_codes": [person.participant_code]
            },
            "observation_cutoff": "2026-08-28T03:00:00Z",
            "calendar_cutoff": "2026-08-28T03:00:00Z",
        },
    )
    assert snapshot.status_code == 201
    snapshot_id = snapshot.json()["id"]
    items = browser.get(
        f"/admin/api/research/dataset-snapshots/{snapshot_id}/items"
    )
    assert items.status_code == 200
    assert {item["item_type"] for item in items.json()["items"]} >= {
        "participant", "observation", "forecast", "match_source"
    }
    offline = browser.post(
        "/admin/api/research/evaluation-runs",
        headers={"X-CSRF-Token": auth["csrf_token"]},
        json={
            "dataset_snapshot_id": snapshot_id,
            "model_version": "candidate.v1",
            "evaluation_mode": "offline_replay",
        },
    )
    assert offline.status_code == 201
    assert offline.json()["status"] == "completed"
    script = browser.get("/admin/static/app.js").text
    for marker in (
        "研究评估",
        "数据质量",
        "研究诊断",
        "参数历史",
        "/research/dataset-snapshots",
        "/research/evaluation-runs",
        "ADMIN MODEL COMPARISON",
        "/research/model-comparison",
        "historical_online",
        "EMA 观测日率",
    ):
        assert marker in script
