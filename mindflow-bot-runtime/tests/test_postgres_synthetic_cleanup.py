"""Opt-in PostgreSQL proofs for cleanup dependency planning and execution."""

from datetime import date, datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import select, text

from app.db import Base, Database, build_engine
from app.models import (
    CareInterventionEvent,
    CareInterventionFeedback,
    CareInterventionOutcome,
    DailyReviewResponse,
    EventAppraisalFeedback,
    ForecastCurrentnessEvent,
    ForecastObservationMatch,
    ForecastSnapshot,
    Participant,
    RetrospectiveCurveSnapshot,
    StateObservation,
    WarningSchedule,
)
from app.synthetic_data import CleanupPlanError, audit_synthetic_data, cleanup_from_plan
from postgres_test_guard import (
    get_test_postgres_connect_timeout_seconds,
    optional_test_postgres_url,
)


@pytest.fixture
def postgres_cleanup_database():
    try:
        raw_url = optional_test_postgres_url()
    except ValueError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("MINDFLOW_TEST_POSTGRES_URL is not configured")
    schema = f"mindflow_cleanup_{uuid.uuid4().hex}"
    root_engine = build_engine(
        raw_url,
        connect_timeout_seconds=get_test_postgres_connect_timeout_seconds(),
    )
    with root_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = root_engine.execution_options(schema_translate_map={None: schema})
    Base.metadata.create_all(scoped_engine)
    try:
        yield Database(scoped_engine)
    finally:
        scoped_engine.dispose()
        with root_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        root_engine.dispose()


def _forecast(session, participant_id, local_date, version):
    row = ForecastSnapshot(
        participant_id=participant_id,
        local_date=local_date,
        calendar_revision=version,
        semantic_revision=version,
        observation_revision=version,
        algorithm_version="forecast.v4",
        forecast_version=version,
        semantic_status="complete",
        semantic_input_json=[],
        curve_json=[],
        peaks_json=[],
        warning_windows_json=[],
        output_json={"source": version},
        valid=True,
    )
    session.add(row)
    session.flush()
    return row


def _warning(session, participant_id, forecast, local_date, *, status="pending"):
    stamp = datetime.combine(local_date, datetime.min.time(), tzinfo=timezone.utc)
    row = WarningSchedule(
        participant_id=participant_id,
        local_date=local_date,
        forecast_id=forecast.id,
        forecast_version=forecast.forecast_version,
        warning_identity=f"warning-{uuid.uuid4().hex}",
        episode_identity=f"episode-{uuid.uuid4().hex}",
        target_time=stamp + timedelta(hours=8),
        risk_time=stamp + timedelta(hours=9),
        valid_until=stamp + timedelta(hours=10),
        warning_level="medium",
        status=status,
        payload_json={},
    )
    session.add(row)
    session.flush()
    return row


def _care(session, participant_id, forecast, warning, scheduled_at, *, marker):
    row = CareInterventionEvent(
        participant_id=participant_id,
        source_warning_id=warning.id,
        source_forecast_id=forecast.id,
        forecast_version=forecast.forecast_version,
        intervention_type="warning",
        template_id=marker,
        template_version="1",
        reason_code=marker,
        scheduled_at=scheduled_at,
        status="pending",
        delivery_status="pending",
        message_text="fixture",
        context_json={"source": marker},
        actions_json=[],
    )
    session.add(row)
    session.flush()
    return row


def test_postgres_cleanup_explicitly_plans_and_deletes_cascade_rows(postgres_cleanup_database):
    database = postgres_cleanup_database
    local_date = date(2035, 3, 4)
    with database.session() as session:
        user = Participant(participant_code="TEST-PG-CASCADE")
        session.add(user)
        session.flush()
        forecast = _forecast(session, user.id, local_date, "synthetic-pg-cascade")
        warning = _warning(session, user.id, forecast, local_date)
        care = _care(
            session, user.id, forecast, warning,
            datetime(2035, 3, 4, 8, tzinfo=timezone.utc), marker="synthetic-pg-cascade",
        )
        currentness = ForecastCurrentnessEvent(
            participant_id=user.id,
            local_date=local_date,
            forecast_id=forecast.id,
            forecast_version=forecast.forecast_version,
            event_type="activated",
            reason="fixture",
            occurred_at=datetime(2035, 3, 4, tzinfo=timezone.utc),
        )
        feedback = CareInterventionFeedback(
            intervention_id=care.id,
            participant_id=user.id,
            action_selected="helpful",
            callback_event_id=f"callback-{uuid.uuid4().hex}",
        )
        outcome = CareInterventionOutcome(
            intervention_id=care.id,
            participant_id=user.id,
            baseline_state={"stress": 7, "energy": 3},
            context_json={"source": "synthetic-pg-cascade"},
        )
        observed_at = datetime(2035, 3, 4, 9, tzinfo=timezone.utc)
        observation = StateObservation(
            participant_id=user.id,
            observation_type="ema",
            source_message_id=f"synthetic-pg-observation-{uuid.uuid4().hex}",
            payload_json={"stress": 6},
            observed_at=observed_at,
        )
        session.add(observation)
        session.flush()
        match = ForecastObservationMatch(
            participant_id=user.id,
            local_date=local_date,
            forecast_id=forecast.id,
            forecast_version=forecast.forecast_version,
            match_schema_version="synthetic-pg-match-v1",
            forecast_timestamp=observed_at,
            observation_id=observation.id,
            observed_at=observed_at,
            predicted_stress=5.5,
            actual_stress=6,
            residual=0.5,
            context_json={"source": "synthetic-pg-cascade"},
        )
        session.add_all([currentness, feedback, outcome, match])
        session.flush()
        ids = (
            forecast.id,
            warning.id,
            care.id,
            currentness.id,
            feedback.id,
            match.id,
        )

    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    planned = {(row["table"], row["id"]) for row in report["cleanup_plan"]["rows"]}
    assert ("forecast_currentness_events", str(ids[3])) in planned
    assert ("care_intervention_feedback", str(ids[4])) in planned
    assert ("forecast_observation_matches", str(ids[5])) in planned
    assert ("care_intervention_outcomes", str(ids[2])) in planned
    assert report["cleanup_plan"]["expected_cleanup_counts"][
        "care_intervention_outcomes"
    ] == 1
    assert report["cleanup_plan"]["expected_cleanup_counts"][
        "forecast_observation_matches"
    ] == 1
    cleanup_from_plan(
        database.engine, report["cleanup_plan"], execute=True, backup_confirmed=True
    )
    with database.session() as session:
        assert session.get(ForecastSnapshot, ids[0]) is None
        assert session.get(WarningSchedule, ids[1]) is None
        assert session.get(CareInterventionEvent, ids[2]) is None
        assert session.get(ForecastCurrentnessEvent, ids[3]) is None
        assert session.get(CareInterventionFeedback, ids[4]) is None
        assert session.get(CareInterventionOutcome, ids[2]) is None
        assert session.get(ForecastObservationMatch, ids[5]) is None


def test_postgres_audit_blocks_set_null_and_restrict_dependencies(postgres_cleanup_database):
    database = postgres_cleanup_database
    with database.session() as session:
        user = Participant(participant_code="P-PG-DEPENDENCY")
        session.add(user)
        session.flush()
        restricted = _forecast(session, user.id, date(2036, 4, 5), "synthetic-pg-restrict")
        ordinary = _forecast(session, user.id, date(2026, 8, 28), "production-pg-care")
        source_warning = _warning(session, user.id, ordinary, date(2026, 8, 28), status="cancelled")
        care = _care(
            session, user.id, ordinary, source_warning,
            datetime(2026, 8, 28, 8, tzinfo=timezone.utc), marker="synthetic-care-only",
        )
        snoozed = _warning(session, user.id, ordinary, date(2026, 8, 29), status="cancelled")
        snoozed.snoozed_from_intervention_id = care.id
        response = DailyReviewResponse(
            participant_id=user.id,
            local_date=restricted.local_date,
            revision=1,
            card_version="v1",
            causal_source_forecast_id=restricted.id,
            causal_source_forecast_version=restricted.forecast_version,
            callback_event_id=f"review-{uuid.uuid4().hex}",
            submitted_at=datetime(2036, 4, 5, 14, tzinfo=timezone.utc),
            start_stress=3, start_energy=7, peak_stress=6,
            peak_period="afternoon", end_stress=4, end_energy=5,
            energy_consumption=2, raw_json={},
        )
        appraisal = EventAppraisalFeedback(
            participant_id=user.id,
            event_id="synthetic-pg-appraisal",
            mental_demand=8,
            physical_demand=2,
            temporal_demand=7,
            effort=8,
            frustration=6,
            perceived_control=4,
            actual_stress=7,
            perceived_performance=6,
            source_forecast_id=restricted.id,
            source_forecast_version=restricted.forecast_version,
            submitted_at=datetime(2036, 4, 5, 13, tzinfo=timezone.utc),
        )
        session.add_all([response, appraisal])
        session.flush()
        retrospective = RetrospectiveCurveSnapshot(
            participant_id=user.id,
            local_date=restricted.local_date,
            source_forecast_id=restricted.id,
            source_forecast_version=restricted.forecast_version,
            daily_review_response_id=response.id,
            daily_review_revision=1,
            observation_revision="observation-v1",
            algorithm_version="retrospective.v1",
            reconstruction_version=f"reconstruction-{uuid.uuid4().hex}",
            curve_json=[], analysis_json={}, diagnostics_json={},
        )
        session.add(retrospective)
        session.flush()
        restricted_id = restricted.id
        expected = snoozed.id, response.id, retrospective.id, appraisal.id

    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    assert {row["id"] for row in report["dependent_impacts"]["set_null"]} == {
        str(expected[0]), str(expected[3])
    }
    assert any(
        row["table"] == "event_appraisal_feedback"
        and row["id"] == str(expected[3])
        for row in report["dependent_impacts"]["set_null"]
    )
    assert {row["id"] for row in report["dependent_impacts"]["restrict_blockers"]} == {
        str(expected[1]), str(expected[2])
    }
    restricted_candidate = next(
        row for row in report["candidates"] if row["id"] == str(restricted_id)
    )
    assert restricted_candidate["eligible_for_cleanup"] is False
    assert restricted_candidate["cleanup_blocked"] is True
    assert "set_null_dependency_blocks_cleanup" in restricted_candidate["reasons"]
    for execute in (False, True):
        with pytest.raises(CleanupPlanError, match="SET NULL|RESTRICT"):
            cleanup_from_plan(
                database.engine,
                report["cleanup_plan"],
                execute=execute,
                backup_confirmed=execute,
            )
    with database.session() as session:
        assert session.get(EventAppraisalFeedback, expected[3]).source_forecast_id == (
            restricted_id
        )


def test_postgres_audit_separates_legacy_and_current_authorization_gaps_read_only(
    postgres_cleanup_database,
):
    database = postgres_cleanup_database
    local_date = date(2026, 8, 28)
    legacy_sent_at = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)
    enforcement_sent_at = datetime(2026, 8, 28, 9, tzinfo=timezone.utc)
    current_sent_at = datetime(2026, 8, 28, 10, tzinfo=timezone.utc)
    with database.session() as session:
        user = Participant(participant_code="P-PG-AUTHORIZATION")
        session.add(user)
        session.flush()
        forecast = _forecast(
            session, user.id, local_date, "production-pg-authorization"
        )
        legacy = _warning(session, user.id, forecast, local_date, status="sent")
        baseline = _warning(session, user.id, forecast, local_date, status="sent")
        current = _warning(session, user.id, forecast, local_date, status="sent")
        legacy.authorized_at = None
        legacy.sent_at = legacy_sent_at
        baseline.authorized_at = enforcement_sent_at - timedelta(minutes=1)
        baseline.sent_at = enforcement_sent_at
        current.authorized_at = None
        current.sent_at = current_sent_at
        session.flush()
        legacy_id, baseline_id, current_id = legacy.id, baseline.id, current.id

    report = audit_synthetic_data(database.engine, today=local_date)
    legacy_rows = {
        row["id"]: row
        for row in report["invariants"][
            "legacy_sent_warnings_without_authorization"
        ]
    }
    current_rows = {
        row["id"]: row
        for row in report["invariants"][
            "sent_warnings_without_authorization_or_sent_time"
        ]
    }
    planned_ids = {row["id"] for row in report["cleanup_plan"]["rows"]}

    assert legacy_rows[str(legacy_id)]["classification"] == (
        "legacy_pre_observed_authorization_enforcement"
    )
    assert current_rows[str(current_id)]["reason"] == (
        "missing_authorization_after_enforcement"
    )
    assert {str(legacy_id), str(baseline_id), str(current_id)}.isdisjoint(
        planned_ids
    )

    with database.session() as session:
        stored_legacy = session.get(WarningSchedule, legacy_id)
        stored_baseline = session.get(WarningSchedule, baseline_id)
        stored_current = session.get(WarningSchedule, current_id)
        assert stored_legacy.authorized_at is None
        assert stored_legacy.sent_at == legacy_sent_at
        assert stored_baseline.authorized_at == (
            enforcement_sent_at - timedelta(minutes=1)
        )
        assert stored_baseline.sent_at == enforcement_sent_at
        assert stored_current.authorized_at is None
        assert stored_current.sent_at == current_sent_at
