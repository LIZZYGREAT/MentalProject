from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.models import (
    CareInterventionEvent,
    CareInterventionFeedback,
    CareInterventionOutcome,
    DailyReviewResponse,
    EventAppraisalFeedback,
    ForecastCurrentnessEvent,
    ForecastObservationMatch,
    ForecastSnapshot,
    RetrospectiveCurveSnapshot,
    StateObservation,
    WarningSchedule,
)
from app.synthetic_data import (
    CleanupPlanError,
    approve_cleanup_candidates,
    audit_synthetic_data,
    cleanup_from_plan,
)
from helpers import memory_database, participant


def _forecast(database, participant_id, local_date, *, marker: str, valid: bool = True):
    row = ForecastSnapshot(
        participant_id=participant_id,
        local_date=local_date,
        calendar_revision=marker,
        semantic_revision=marker,
        observation_revision=marker,
        algorithm_version="forecast.v4",
        forecast_version=f"{marker}-{uuid.uuid4().hex[:8]}",
        semantic_status="complete",
        semantic_input_json=[],
        curve_json=[],
        peaks_json=[],
        warning_windows_json=[],
        output_json={"provenance": marker},
        valid=valid,
    )
    with database.session() as session:
        session.add(row)
        session.flush()
        row_id = row.id
    return row_id


def _forecast_match(session, participant_id, forecast_id, local_date, *, marker):
    forecast = session.get(ForecastSnapshot, forecast_id)
    observed_at = datetime.combine(
        local_date, datetime.min.time(), tzinfo=timezone.utc
    ) + timedelta(hours=9)
    observation = StateObservation(
        participant_id=participant_id,
        observation_type="ema",
        source_message_id=f"{marker}-observation-{uuid.uuid4().hex}",
        payload_json={"stress": 6},
        observed_at=observed_at,
    )
    session.add(observation)
    session.flush()
    match = ForecastObservationMatch(
        participant_id=participant_id,
        local_date=local_date,
        forecast_id=forecast_id,
        forecast_version=forecast.forecast_version,
        match_schema_version=f"{marker}-schema",
        forecast_timestamp=observed_at,
        observation_id=observation.id,
        observed_at=observed_at,
        predicted_stress=5.5,
        actual_stress=6,
        residual=0.5,
        context_json={"source": marker},
    )
    session.add(match)
    session.flush()
    return match.id


def _warning_and_care(database, participant_id, forecast_id, local_date, *, marker: str):
    risk_time = datetime.combine(local_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=9)
    warning = WarningSchedule(
        participant_id=participant_id,
        local_date=local_date,
        forecast_id=forecast_id,
        forecast_version=marker,
        warning_identity=f"{marker}-warning",
        episode_identity=f"{marker}-episode",
        target_time=risk_time - timedelta(minutes=20),
        risk_time=risk_time,
        valid_until=risk_time + timedelta(hours=1),
        warning_level="medium",
        status="pending",
        payload_json={"fixture": marker},
    )
    with database.session() as session:
        session.add(warning)
        session.flush()
        care = CareInterventionEvent(
            participant_id=participant_id,
            source_warning_id=warning.id,
            source_forecast_id=forecast_id,
            forecast_version=marker,
            intervention_type="warning",
            template_id=f"{marker}-template",
            template_version="1",
            reason_code=marker,
            scheduled_at=warning.target_time,
            status="pending",
            delivery_status="pending",
            message_text="fixture message",
            context_json={"source": marker},
            actions_json=[],
        )
        session.add(care)
        session.flush()
        return warning.id, care.id


def _counts(database):
    with database.session() as session:
        return {
            "forecast_snapshots": session.scalar(select(func.count()).select_from(ForecastSnapshot)),
            "warning_schedules": session.scalar(select(func.count()).select_from(WarningSchedule)),
            "care_intervention_events": session.scalar(select(func.count()).select_from(CareInterventionEvent)),
        }


def test_audit_combines_evidence_and_never_deletes_on_far_future_date_alone():
    database = memory_database()
    real_user = participant(database, "P-REAL-001")
    synthetic_user = participant(database, "TEST-SYNTHETIC-001")
    far_future = date(2042, 1, 1)
    legitimate_id = _forecast(database, real_user.id, far_future, marker="production-v4")
    synthetic_id = _forecast(database, synthetic_user.id, far_future, marker="forecast-v4")
    warning_id, care_id = _warning_and_care(
        database, synthetic_user.id, synthetic_id, far_future, marker="ordinary-metadata"
    )

    before = _counts(database)
    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    after = _counts(database)

    assert after == before, "the audit must be strictly read-only"
    candidates = {row["id"]: row for row in report["candidates"]}
    assert candidates[str(legitimate_id)]["reasons"] == ["far_future_date"]
    assert candidates[str(legitimate_id)]["eligible_for_cleanup"] is False
    assert candidates[str(synthetic_id)]["eligible_for_cleanup"] is True
    assert "source_forecast_confirmed_synthetic" in candidates[str(warning_id)]["reasons"]
    assert "source_warning_confirmed_synthetic" in candidates[str(care_id)]["reasons"]
    planned_ids = {row["id"] for row in report["cleanup_plan"]["rows"]}
    assert str(legitimate_id) not in planned_ids
    assert {str(synthetic_id), str(warning_id), str(care_id)} <= planned_ids
    assert report["tables"][0]["date_range"] == {"min": "2042-01-01", "max": "2042-01-01"}


def test_cleanup_is_a_real_transaction_with_dry_run_and_explicit_execute():
    database = memory_database()
    synthetic_user = participant(database, "PYTEST-CLEANUP-001")
    local_date = date(2038, 4, 2)
    forecast_id = _forecast(database, synthetic_user.id, local_date, marker="pytest-fixture")
    _warning_and_care(database, synthetic_user.id, forecast_id, local_date, marker="pytest-fixture")
    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    plan = report["cleanup_plan"]
    before = _counts(database)

    dry_run = cleanup_from_plan(database.engine, plan)
    assert dry_run["committed"] is False
    assert _counts(database) == before

    with pytest.raises(CleanupPlanError, match="backup-confirmed"):
        cleanup_from_plan(database.engine, plan, execute=True)

    executed = cleanup_from_plan(database.engine, plan, execute=True, backup_confirmed=True)
    assert executed["committed"] is True
    assert _counts(database) == {
        "forecast_snapshots": 0,
        "warning_schedules": 0,
        "care_intervention_events": 0,
    }


def test_cleanup_rejects_tampered_or_stale_plans_and_rolls_back():
    database = memory_database()
    synthetic_user = participant(database, "FIXTURE-STALE-001")
    local_date = date(2039, 5, 3)
    forecast_id = _forecast(database, synthetic_user.id, local_date, marker="synthetic-fixture")
    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))

    tampered = deepcopy(report["cleanup_plan"])
    tampered["rows"][0]["reasons"] = ["far_future_date"]
    with pytest.raises(CleanupPlanError, match="digest"):
        cleanup_from_plan(database.engine, tampered)

    with database.session() as session:
        session.execute(delete(ForecastSnapshot).where(ForecastSnapshot.id == forecast_id))
    with pytest.raises(CleanupPlanError, match="changed after audit"):
        cleanup_from_plan(database.engine, report["cleanup_plan"], execute=True, backup_confirmed=True)
    assert _counts(database)["forecast_snapshots"] == 0


@pytest.mark.parametrize("obsolete_schema_version", [2, 3])
def test_cleanup_rejects_obsolete_schema_v2_and_v3_plans(
    obsolete_schema_version,
):
    database = memory_database()
    synthetic_user = participant(database, "FIXTURE-SCHEMA-V2")
    _forecast(
        database,
        synthetic_user.id,
        date(2039, 5, 3),
        marker="synthetic-schema-v2",
    )
    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    assert report["schema_version"] == 4
    assert report["cleanup_plan"]["schema_version"] == 4
    obsolete_plan = deepcopy(report["cleanup_plan"])
    obsolete_plan["schema_version"] = obsolete_schema_version

    with pytest.raises(CleanupPlanError, match="unsupported cleanup plan schema"):
        cleanup_from_plan(database.engine, obsolete_plan)


def test_audit_reports_forecast_and_warning_invariant_violations():
    database = memory_database()
    user = participant(database, "P-REAL-002")
    local_date = date(2026, 8, 28)
    first_id = _forecast(database, user.id, local_date, marker="production-a", valid=True)
    _forecast(database, user.id, local_date, marker="production-b", valid=True)
    with database.session() as session:
        warning = WarningSchedule(
            participant_id=user.id,
            local_date=local_date,
            forecast_id=first_id,
            forecast_version="production-a",
            warning_identity="ordinary-warning",
            episode_identity="ordinary-episode",
            target_time=datetime(2026, 8, 28, 2, tzinfo=timezone.utc),
            risk_time=datetime(2026, 8, 28, 3, tzinfo=timezone.utc),
            valid_until=datetime(2026, 8, 28, 4, tzinfo=timezone.utc),
            warning_level="medium",
            status="sent",
            payload_json={},
            authorized_at=None,
            sent_at=None,
        )
        session.add(warning)

    report = audit_synthetic_data(database.engine, today=local_date)
    assert len(report["invariants"]["duplicate_valid_forecasts"]) == 1
    assert len(report["invariants"]["sent_warnings_without_authorization_or_sent_time"]) == 1


def _warning(database, participant_id, forecast_id, local_date, *, status, forecast_version):
    timestamp = datetime.combine(local_date, datetime.min.time(), tzinfo=timezone.utc)
    row = WarningSchedule(
        participant_id=participant_id,
        local_date=local_date,
        forecast_id=forecast_id,
        forecast_version=forecast_version,
        warning_identity=f"warning-{uuid.uuid4().hex}",
        episode_identity=f"episode-{uuid.uuid4().hex}",
        target_time=timestamp + timedelta(hours=8),
        risk_time=timestamp + timedelta(hours=9),
        valid_until=timestamp + timedelta(hours=10),
        warning_level="medium",
        status=status,
        payload_json={},
    )
    with database.session() as session:
        session.add(row)
        session.flush()
        return row.id


def _set_warning_delivery(database, warning_id, *, authorized_at, sent_at):
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        row.authorized_at = authorized_at
        row.sent_at = sent_at


def test_audit_uses_production_warning_status_and_complete_invariants():
    database = memory_database()
    user = participant(database, "P-REAL-INVARIANTS")
    local_date = date(2026, 8, 28)
    stale_id = _forecast(database, user.id, local_date, marker="production-stale", valid=False)
    stale_warning = _warning(
        database, user.id, stale_id, local_date,
        status="delivery_unavailable", forecast_version="production-stale",
    )
    valid_id = _forecast(database, user.id, local_date + timedelta(days=1), marker="production-current")
    mismatch_warning = _warning(
        database, user.id, valid_id, local_date + timedelta(days=1),
        status="pending", forecast_version="wrong-version",
    )
    sent_warning = _warning(
        database, user.id, valid_id, local_date + timedelta(days=1),
        status="sent", forecast_version="production-current",
    )
    with database.session() as session:
        row = session.get(WarningSchedule, sent_warning)
        row.authorized_at = datetime(2026, 8, 28, 10, tzinfo=timezone.utc)
        row.sent_at = datetime(2026, 8, 28, 9, tzinfo=timezone.utc)

    report = audit_synthetic_data(database.engine, today=local_date)
    stale = {row["warning_id"] for row in report["invariants"]["active_warnings_on_stale_forecasts"]}
    invalid_sent = {row["id"] for row in report["invariants"]["sent_warnings_without_authorization_or_sent_time"]}

    assert {str(stale_warning), str(mismatch_warning)} <= stale
    assert str(sent_warning) in invalid_sent


def test_audit_classifies_missing_authorization_before_observed_enforcement_as_legacy():
    database = memory_database()
    user = participant(database, "P-REAL-AUTH-LEGACY")
    local_date = date(2026, 8, 28)
    forecast_id = _forecast(
        database, user.id, local_date, marker="production-auth-boundary"
    )
    legacy_id = _warning(
        database,
        user.id,
        forecast_id,
        local_date,
        status="sent",
        forecast_version="production-auth-boundary",
    )
    baseline_id = _warning(
        database,
        user.id,
        forecast_id,
        local_date,
        status="sent",
        forecast_version="production-auth-boundary",
    )
    legacy_sent_at = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)
    baseline_sent_at = datetime(2026, 8, 28, 9, tzinfo=timezone.utc)
    _set_warning_delivery(
        database, legacy_id, authorized_at=None, sent_at=legacy_sent_at
    )
    _set_warning_delivery(
        database,
        baseline_id,
        authorized_at=baseline_sent_at - timedelta(minutes=1),
        sent_at=baseline_sent_at,
    )

    report = audit_synthetic_data(database.engine, today=local_date)
    legacy = {
        row["id"]: row
        for row in report["invariants"][
            "legacy_sent_warnings_without_authorization"
        ]
    }
    current_ids = {
        row["id"]
        for row in report["invariants"][
            "sent_warnings_without_authorization_or_sent_time"
        ]
    }

    assert legacy[str(legacy_id)]["classification"] == (
        "legacy_pre_observed_authorization_enforcement"
    )
    assert str(legacy_id) not in current_ids
    assert str(legacy_id) not in {
        row["id"] for row in report["cleanup_plan"]["rows"]
    }


def test_audit_reports_post_enforcement_missing_authorization_as_current_violation():
    database = memory_database()
    user = participant(database, "P-REAL-AUTH-CURRENT")
    local_date = date(2026, 8, 28)
    forecast_id = _forecast(
        database, user.id, local_date, marker="production-auth-current"
    )
    baseline_id = _warning(
        database, user.id, forecast_id, local_date,
        status="sent", forecast_version="production-auth-current",
    )
    current_id = _warning(
        database, user.id, forecast_id, local_date,
        status="sent", forecast_version="production-auth-current",
    )
    baseline_sent_at = datetime(2026, 8, 28, 9, tzinfo=timezone.utc)
    _set_warning_delivery(
        database, baseline_id,
        authorized_at=baseline_sent_at - timedelta(minutes=1),
        sent_at=baseline_sent_at,
    )
    _set_warning_delivery(
        database, current_id, authorized_at=None,
        sent_at=baseline_sent_at + timedelta(minutes=1),
    )

    report = audit_synthetic_data(database.engine, today=local_date)
    current = {
        row["id"]: row
        for row in report["invariants"][
            "sent_warnings_without_authorization_or_sent_time"
        ]
    }
    legacy_ids = {
        row["id"]
        for row in report["invariants"][
            "legacy_sent_warnings_without_authorization"
        ]
    }

    assert current[str(current_id)]["reason"] == (
        "missing_authorization_after_enforcement"
    )
    assert str(current_id) not in legacy_ids


def test_audit_always_reports_sent_warning_without_sent_time_as_current_violation():
    database = memory_database()
    user = participant(database, "P-REAL-AUTH-MISSING-SENT")
    local_date = date(2026, 8, 28)
    forecast_id = _forecast(
        database, user.id, local_date, marker="production-missing-sent"
    )
    warning_id = _warning(
        database, user.id, forecast_id, local_date,
        status="sent", forecast_version="production-missing-sent",
    )
    _set_warning_delivery(
        database,
        warning_id,
        authorized_at=datetime(2026, 8, 28, 9, tzinfo=timezone.utc),
        sent_at=None,
    )

    report = audit_synthetic_data(database.engine, today=local_date)
    current = {
        row["id"]: row
        for row in report["invariants"][
            "sent_warnings_without_authorization_or_sent_time"
        ]
    }

    assert current[str(warning_id)]["reason"] == "missing_sent_at"


def test_audit_always_reports_warning_sent_before_authorization_as_current_violation():
    database = memory_database()
    user = participant(database, "P-REAL-AUTH-ORDER")
    local_date = date(2026, 8, 28)
    forecast_id = _forecast(
        database, user.id, local_date, marker="production-auth-order"
    )
    warning_id = _warning(
        database, user.id, forecast_id, local_date,
        status="sent", forecast_version="production-auth-order",
    )
    sent_at = datetime(2026, 8, 28, 9, tzinfo=timezone.utc)
    _set_warning_delivery(
        database,
        warning_id,
        authorized_at=sent_at + timedelta(minutes=1),
        sent_at=sent_at,
    )

    report = audit_synthetic_data(database.engine, today=local_date)
    current = {
        row["id"]: row
        for row in report["invariants"][
            "sent_warnings_without_authorization_or_sent_time"
        ]
    }

    assert current[str(warning_id)]["reason"] == "sent_before_authorization"


def test_audit_fails_closed_when_no_authorization_enforcement_baseline_exists():
    database = memory_database()
    user = participant(database, "P-REAL-AUTH-NO-BASELINE")
    local_date = date(2026, 8, 28)
    forecast_id = _forecast(
        database, user.id, local_date, marker="production-no-baseline"
    )
    warning_id = _warning(
        database, user.id, forecast_id, local_date,
        status="sent", forecast_version="production-no-baseline",
    )
    original_sent_at = datetime(2026, 8, 28, 9, tzinfo=timezone.utc)
    _set_warning_delivery(
        database, warning_id, authorized_at=None, sent_at=original_sent_at
    )

    report = audit_synthetic_data(database.engine, today=local_date)
    current = {
        row["id"]: row
        for row in report["invariants"][
            "sent_warnings_without_authorization_or_sent_time"
        ]
    }

    assert report["invariants"]["legacy_sent_warnings_without_authorization"] == []
    assert current[str(warning_id)]["reason"] == (
        "missing_authorization_without_enforcement_baseline"
    )
    with database.session() as session:
        unchanged = session.get(WarningSchedule, warning_id)
        assert unchanged.authorized_at is None
        assert unchanged.sent_at == original_sent_at.replace(tzinfo=None)


def test_audit_plans_cascade_dependents_explicitly_and_cleanup_deletes_them():
    database = memory_database()
    user = participant(database, "TEST-DEPENDENCIES")
    local_date = date(2035, 3, 4)
    forecast_id = _forecast(database, user.id, local_date, marker="synthetic-dependency")
    warning_id, care_id = _warning_and_care(
        database, user.id, forecast_id, local_date, marker="synthetic-dependency"
    )
    with database.session() as session:
        currentness = ForecastCurrentnessEvent(
            participant_id=user.id,
            local_date=local_date,
            forecast_id=forecast_id,
            forecast_version="synthetic-dependency",
            event_type="activated",
            reason="synthetic fixture",
            occurred_at=datetime(2035, 3, 4, tzinfo=timezone.utc),
        )
        feedback = CareInterventionFeedback(
            intervention_id=care_id,
            participant_id=user.id,
            action_selected="helpful",
            callback_event_id=f"callback-{uuid.uuid4().hex}",
        )
        outcome = CareInterventionOutcome(
            intervention_id=care_id,
            participant_id=user.id,
            baseline_state={"stress": 7, "energy": 3},
            context_json={"source": "synthetic-dependency"},
        )
        match_id = _forecast_match(
            session,
            user.id,
            forecast_id,
            local_date,
            marker="synthetic-dependency",
        )
        session.add_all([currentness, feedback, outcome])
        session.flush()
        currentness_id, feedback_id = currentness.id, feedback.id

    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    impacts = report["dependent_impacts"]["cascade_delete"]
    planned = {(row["table"], row["id"]) for row in report["cleanup_plan"]["rows"]}
    assert ("forecast_currentness_events", str(currentness_id)) in planned
    assert ("forecast_observation_matches", str(match_id)) in planned
    assert ("care_intervention_feedback", str(feedback_id)) in planned
    assert ("care_intervention_outcomes", str(care_id)) in planned
    assert report["cleanup_plan"]["expected_cleanup_counts"][
        "care_intervention_outcomes"
    ] == 1
    assert report["cleanup_plan"]["expected_cleanup_counts"][
        "forecast_observation_matches"
    ] == 1
    assert {impact["planned_action"] for impact in impacts} == {"explicit_delete"}

    cleanup_from_plan(
        database.engine, report["cleanup_plan"], execute=True, backup_confirmed=True
    )
    with database.session() as session:
        assert session.get(ForecastCurrentnessEvent, currentness_id) is None
        assert session.get(ForecastObservationMatch, match_id) is None
        assert session.get(CareInterventionFeedback, feedback_id) is None
        assert session.get(CareInterventionOutcome, care_id) is None
        assert session.get(WarningSchedule, warning_id) is None


def test_cleanup_rejects_forecast_match_changes_after_audit():
    database = memory_database()
    user = participant(database, "TEST-MATCH-STALE")
    local_date = date(2035, 3, 5)
    forecast_id = _forecast(
        database, user.id, local_date, marker="synthetic-match-stale"
    )
    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    with database.session() as session:
        _forecast_match(
            session,
            user.id,
            forecast_id,
            local_date,
            marker="synthetic-match-stale",
        )

    for execute in (False, True):
        with pytest.raises(
            CleanupPlanError,
            match="CASCADE dependencies changed after audit",
        ):
            cleanup_from_plan(
                database.engine,
                report["cleanup_plan"],
                execute=execute,
                backup_confirmed=execute,
            )


def test_event_appraisal_feedback_blocks_forecast_cleanup():
    database = memory_database()
    user = participant(database, "TEST-APPRAISAL-BLOCK")
    local_date = date(2035, 3, 6)
    forecast_id = _forecast(
        database, user.id, local_date, marker="synthetic-appraisal-block"
    )
    with database.session() as session:
        appraisal = EventAppraisalFeedback(
            participant_id=user.id,
            event_id="synthetic-appraisal-event",
            mental_demand=8,
            physical_demand=2,
            temporal_demand=7,
            effort=8,
            frustration=6,
            perceived_control=4,
            actual_stress=7,
            perceived_performance=6,
            source_forecast_id=forecast_id,
            source_forecast_version="synthetic-appraisal-block",
            submitted_at=datetime(2035, 3, 6, 10, tzinfo=timezone.utc),
        )
        session.add(appraisal)
        session.flush()
        appraisal_id = appraisal.id

    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    set_null = report["dependent_impacts"]["set_null"]
    candidate = next(
        row for row in report["candidates"] if row["id"] == str(forecast_id)
    )
    planned = {(row["table"], row["id"]) for row in report["cleanup_plan"]["rows"]}

    assert any(
        impact["table"] == "event_appraisal_feedback"
        and impact["id"] == str(appraisal_id)
        for impact in set_null
    )
    assert candidate["eligible_for_cleanup"] is False
    assert candidate["cleanup_blocked"] is True
    assert "set_null_dependency_blocks_cleanup" in candidate["reasons"]
    assert ("forecast_snapshots", str(forecast_id)) not in planned


def test_cleanup_rejects_appraisal_added_after_audit_without_nulling_provenance():
    database = memory_database()
    user = participant(database, "TEST-APPRAISAL-STALE")
    local_date = date(2035, 3, 7)
    forecast_id = _forecast(
        database, user.id, local_date, marker="synthetic-appraisal-stale"
    )
    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    with database.session() as session:
        appraisal = EventAppraisalFeedback(
            participant_id=user.id,
            event_id="synthetic-appraisal-stale-event",
            mental_demand=8,
            physical_demand=2,
            temporal_demand=7,
            effort=8,
            frustration=6,
            perceived_control=4,
            actual_stress=7,
            perceived_performance=6,
            source_forecast_id=forecast_id,
            source_forecast_version="synthetic-appraisal-stale",
            submitted_at=datetime(2035, 3, 7, 10, tzinfo=timezone.utc),
        )
        session.add(appraisal)
        session.flush()
        appraisal_id = appraisal.id

    for execute in (False, True):
        with pytest.raises(
            CleanupPlanError,
            match="event appraisal feedback through SET NULL",
        ):
            cleanup_from_plan(
                database.engine,
                report["cleanup_plan"],
                execute=execute,
                backup_confirmed=execute,
            )
    with database.session() as session:
        assert session.get(EventAppraisalFeedback, appraisal_id).source_forecast_id == (
            forecast_id
        )


def test_audit_blocks_set_null_and_restrict_side_effects_before_cleanup():
    database = memory_database()
    user = participant(database, "P-REAL-DEPENDENCY")
    local_date = date(2036, 4, 5)
    forecast_id = _forecast(database, user.id, local_date, marker="synthetic-restrict")
    care_date = date(2026, 8, 28)
    care_forecast_id = _forecast(
        database, user.id, care_date, marker="production-care-forecast"
    )
    source_warning = _warning(
        database, user.id, care_forecast_id, care_date,
        status="cancelled", forecast_version="production-care-forecast",
    )
    with database.session() as session:
        care = CareInterventionEvent(
            participant_id=user.id,
            source_warning_id=source_warning,
            source_forecast_id=care_forecast_id,
            forecast_version="production",
            intervention_type="warning",
            template_id="synthetic-care-only",
            template_version="1",
            reason_code="synthetic-care-only",
            scheduled_at=datetime(2026, 8, 28, 8, tzinfo=timezone.utc),
            status="pending",
            delivery_status="pending",
            message_text="fixture",
            context_json={},
            actions_json=[],
        )
        session.add(care)
        session.flush()
        snoozed_warning = WarningSchedule(
            participant_id=user.id,
            local_date=care_date,
            forecast_id=care_forecast_id,
            forecast_version="production-care-forecast",
            snoozed_from_intervention_id=care.id,
            warning_identity=f"ordinary-{uuid.uuid4().hex}",
            episode_identity=f"ordinary-{uuid.uuid4().hex}",
            target_time=datetime(2026, 8, 28, 8, tzinfo=timezone.utc),
            risk_time=datetime(2026, 8, 28, 9, tzinfo=timezone.utc),
            valid_until=datetime(2026, 8, 28, 10, tzinfo=timezone.utc),
            warning_level="medium",
            status="cancelled",
            payload_json={},
        )
        response = DailyReviewResponse(
            participant_id=user.id,
            local_date=local_date,
            revision=1,
            card_version="v1",
            causal_source_forecast_id=forecast_id,
            causal_source_forecast_version="synthetic-restrict",
            callback_event_id=f"review-{uuid.uuid4().hex}",
            submitted_at=datetime(2036, 4, 5, 14, tzinfo=timezone.utc),
            start_stress=3, start_energy=7, peak_stress=6,
            peak_period="afternoon", end_stress=4, end_energy=5,
            energy_consumption=2, raw_json={},
        )
        session.add_all([snoozed_warning, response])
        session.flush()
        retrospective = RetrospectiveCurveSnapshot(
            participant_id=user.id,
            local_date=local_date,
            source_forecast_id=forecast_id,
            source_forecast_version="synthetic-restrict",
            daily_review_response_id=response.id,
            daily_review_revision=1,
            observation_revision="observation-v1",
            algorithm_version="retrospective.v1",
            reconstruction_version=f"reconstruction-{uuid.uuid4().hex}",
            curve_json=[],
            analysis_json={},
            diagnostics_json={},
        )
        session.add(retrospective)
        session.flush()
        care_id, response_id, retrospective_id = care.id, response.id, retrospective.id

    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    impacts = report["dependent_impacts"]
    assert any(row["id"] == str(snoozed_warning.id) for row in impacts["set_null"])
    assert any(row["id"] == str(response_id) for row in impacts["restrict_blockers"])
    assert any(row["id"] == str(retrospective_id) for row in impacts["restrict_blockers"])
    candidate_by_id = {row["id"]: row for row in report["candidates"]}
    assert candidate_by_id[str(forecast_id)]["cleanup_blocked"] is True
    assert candidate_by_id[str(care_id)]["cleanup_blocked"] is True
    with pytest.raises(CleanupPlanError, match="SET NULL|RESTRICT"):
        cleanup_from_plan(database.engine, report["cleanup_plan"])


def test_operator_approval_promotes_only_audited_candidate_and_reaudits_dependencies():
    database = memory_database()
    user = participant(database, "P-REAL-LEGACY")
    future_date = date(2163, 1, 1)
    forecast_id = _forecast(database, user.id, future_date, marker="production-legacy")
    report = audit_synthetic_data(database.engine, today=date(2026, 8, 28))
    candidate = next(row for row in report["candidates"] if row["id"] == str(forecast_id))
    assert candidate["eligible_for_cleanup"] is False

    plan = approve_cleanup_candidates(database.engine, report, [str(forecast_id)])
    approved = next(row for row in plan["rows"] if row["id"] == str(forecast_id))
    assert "far_future_date" in approved["reasons"]
    assert "operator_approved_after_audit" in approved["reasons"]
    cleanup_from_plan(database.engine, plan, execute=True, backup_confirmed=True)
    assert _counts(database)["forecast_snapshots"] == 0

    tampered = deepcopy(report)
    tampered["candidates"][0]["reasons"] = []
    with pytest.raises(CleanupPlanError, match="audit report digest"):
        approve_cleanup_candidates(database.engine, tampered, [str(forecast_id)])
