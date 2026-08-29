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
from app.services.research_evaluation import ResearchEvaluationService
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
                    observation_type="instant_checkin",
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
                    observation_type="instant_checkin",
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
    assert rebuilt["match_schema_version"] == "forecast-observation-grid.v1"
    assert len(evaluation["matches"]) == 2
    first = evaluation["matches"][0]
    assert first["forecast_version"] == "forecast-stage2-v1"
    assert first["predicted_stress"] == 5.0
    assert first["actual_stress"] == 7.0
    assert first["residual"] == 2.0
    assert first["context"]["event_types"] == ["class"]
    assert first["context"]["courses"] == ["高等数学"]
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

    assert snapshot["schema_version"] == "mindflow-research-dataset-v2"
    assert snapshot["manifest"]["participant_count"] == 1
    assert snapshot["manifest"]["observation_count"] == 2
    assert snapshot["manifest"]["forecast_count"] == 1
    assert snapshot["manifest"]["calendar_count"] == 1
    assert len(snapshot["manifest"]["manifest_hash"]) == 64
    frozen = service.snapshot_items(uuid.UUID(snapshot["id"]))
    assert {item["item_type"] for item in frozen} == {
        "observation", "forecast", "forecast_currentness", "calendar",
        "match_source",
    }
    assert run["dataset_snapshot_id"] == snapshot["id"]
    assert run["model_version"] == "forecast.v4"
    assert run["evaluation_mode"] == "historical_online"
    assert run["status"] == "completed"
    assert run["metrics"]["matched_observation_count"] == 2
    assert run["metrics"]["config"]["manifest_hash"] == snapshot["manifest"][
        "manifest_hash"
    ]
    assert service.list_snapshots()[0]["id"] == snapshot["id"]
    assert service.list_runs()[0]["id"] == run["id"]


def test_longitudinal_parameter_history_and_data_quality_cover_stage2_gates():
    database = memory_database()
    person = participant(database, "synthetic-stage2")
    with database.session() as session:
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
                    observation_type="instant_checkin",
                    source_message_id="late-backfill",
                    payload_json={"stress_0_10": 6.0},
                    observed_at=datetime(2026, 8, 28, 1, tzinfo=timezone.utc),
                    created_at=datetime(2026, 8, 28, 8, tzinfo=timezone.utc),
                ),
                StateObservation(
                    participant_id=person.id,
                    observation_type="instant_checkin",
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
                observation_type="instant_checkin",
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
    assert first_run["metrics"]["config"]["source_set"] == second_run[
        "metrics"
    ]["config"]["source_set"]

    offline = service.create_evaluation_run(
        snapshot_id, "candidate.v1", evaluation_mode="offline_replay"
    )
    assert offline["evaluation_mode"] == "offline_replay"
    assert offline["status"] == "not_implemented"
    assert offline["metrics"]["config"]["dataset_schema_version"] == (
        "mindflow-research-dataset-v2"
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
    assert {item["item_type"] for item in items} == {"observation"}
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
        "observation", "forecast", "match_source"
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
    assert offline.json()["status"] == "not_implemented"
    script = browser.get("/admin/static/app.js").text
    for marker in (
        "研究评估",
        "数据质量",
        "研究诊断",
        "参数历史",
        "/research/dataset-snapshots",
        "/research/evaluation-runs",
        "historical_online",
        "EMA 观测日率",
    ):
        assert marker in script
