from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.models import CareInterventionEvent, ForecastSnapshot, WarningSchedule
from app.synthetic_data import CleanupPlanError, audit_synthetic_data, cleanup_from_plan
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
