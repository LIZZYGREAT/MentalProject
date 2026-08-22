import asyncio
from datetime import date, datetime, timedelta, timezone
import uuid
from zoneinfo import ZoneInfo

from app.integrations.feishu.cards import pressure_curve_card
from app.models import ForecastSnapshot, StateObservation, WarningSchedule
from app.repositories import (
    ForecastSnapshotRepository,
    LearnedProfileRepository,
    ObservationRepository,
    ParticipantRepository,
    WarningScheduleRepository,
)
from app.services.curve_analysis import analyze_curve
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.profile_calibration import ProfileCalibrationService, layered_profile
from app.services.warning_policy import WarningPolicy
from app.tools.care import CareTools
from helpers import memory_database
from utils.alert_monitor import AlertMonitor


DAY = date(2030, 1, 15)
NOW = datetime(2030, 1, 15, 1, 0, tzinfo=timezone.utc)


def _alert(time_value, tier, risk, episode):
    return {
        "time": time_value, "tier": tier, "C": risk, "S": 70 + tier,
        "elevated_auc": tier, "episode_index": episode,
    }


def test_warning_policy_ranks_dedupes_caps_and_never_uses_critical_exception():
    selected = WarningPolicy().select_daily_candidates([
        _alert("09:00", 1, 0.5, 1),
        _alert("10:00", 3, 0.9, 1),  # same episode: higher candidate wins
        _alert("13:00", 2, 0.8, 2),  # too close to the best candidate
        _alert("18:00", 2, 0.7, 3),
        _alert("22:30", 3, 0.95, 4),  # third send is never an exception
    ])
    assert [(row["time"], row["episode_index"]) for row in selected] == [
        ("10:00", 1), ("22:30", 4)
    ]
    assert all(row["warning_policy"]["max_daily_sends"] == 2 for row in selected)


def _forecast(database, participant_id):
    return ForecastSnapshotRepository(database).save(
        participant_id, DAY, calendar_revision="c", semantic_revision="s",
        observation_revision="o", algorithm_version="a", forecast_version="v",
        semantic_status="rules_only", semantic_input=[], curve=[], peaks=[],
        warning_windows=[], output={},
    )


def _versioned_forecast(database, participant_id, version, *, local_date=DAY, curve=None):
    return ForecastSnapshotRepository(database).save(
        participant_id, local_date, calendar_revision=f"c-{version}",
        semantic_revision="s", observation_revision=f"o-{version}",
        algorithm_version="a", forecast_version=version,
        semantic_status="rules_only", semantic_input=[], curve=curve or [],
        peaks=[], warning_windows=[], output={},
    )


def _window(episode, target, valid, risk):
    return {
        "episode_identity": episode, "target_time": target,
        "valid_until": valid, "risk_time": risk, "warning_level": "2",
        "payload": {}, "episode_drift_minutes": 15,
    }


def _warning_row(participant_id, forecast_id, identity, *, target, risk, valid, status="pending", sent_at=None):
    return WarningSchedule(
        participant_id=participant_id, local_date=DAY, forecast_id=forecast_id,
        forecast_version="v", warning_identity=identity,
        episode_identity=identity, target_time=target, risk_time=risk,
        valid_until=valid, warning_level="2", status=status,
        payload_json={}, next_attempt_at=target, sent_at=sent_at,
    )


def test_durable_warning_guard_counts_only_success_and_enforces_cap():
    database = memory_database()
    participant = ParticipantRepository(database).create("POLICY-CAP")
    saved = _forecast(database, participant.id)
    forecast_id = uuid.UUID(saved["id"])
    with database.session() as session:
        session.add_all([
            _warning_row(participant.id, forecast_id, "sent-1", target=NOW, risk=NOW + timedelta(hours=1), valid=NOW + timedelta(minutes=30), status="sent", sent_at=NOW - timedelta(hours=8)),
            _warning_row(participant.id, forecast_id, "sent-2", target=NOW, risk=NOW + timedelta(hours=1), valid=NOW + timedelta(minutes=30), status="escalated", sent_at=NOW - timedelta(hours=4)),
            _warning_row(participant.id, forecast_id, "failed", target=NOW, risk=NOW + timedelta(hours=1), valid=NOW + timedelta(minutes=30), status="failed"),
            _warning_row(participant.id, forecast_id, "candidate", target=NOW, risk=NOW + timedelta(hours=1), valid=NOW + timedelta(minutes=30)),
        ])
    repository = WarningScheduleRepository(database)
    with database.session() as session:
        candidate = session.query(WarningSchedule).filter_by(warning_identity="candidate").one()
        candidate_id = candidate.id
    assert repository.count_successful_deliveries(participant.id, DAY) == 2
    assert repository.claim_if_current(candidate_id, now=NOW) is None
    with database.session() as session:
        row = session.get(WarningSchedule, candidate_id)
        assert row.status == "suppressed"
        assert row.payload_json["suppression_reason"] == "daily_cap"


def test_durable_warning_guard_defers_or_suppresses_minimum_interval():
    database = memory_database()
    participant = ParticipantRepository(database).create("POLICY-INTERVAL")
    saved = _forecast(database, participant.id)
    forecast_id = uuid.UUID(saved["id"])
    sent_at = NOW - timedelta(minutes=60)
    with database.session() as session:
        session.add_all([
            _warning_row(participant.id, forecast_id, "sent", target=NOW, risk=NOW + timedelta(hours=6), valid=NOW + timedelta(hours=5), status="sent", sent_at=sent_at),
            _warning_row(participant.id, forecast_id, "defer", target=NOW, risk=NOW + timedelta(hours=5), valid=NOW + timedelta(hours=4)),
            _warning_row(participant.id, forecast_id, "suppress", target=NOW, risk=NOW + timedelta(hours=2), valid=NOW + timedelta(hours=1)),
        ])
    repository = WarningScheduleRepository(database)
    with database.session() as session:
        ids = {row.warning_identity: row.id for row in session.query(WarningSchedule).all()}
    assert repository.claim_if_current(ids["defer"], now=NOW) is None
    assert repository.claim_if_current(ids["suppress"], now=NOW) is None
    with database.session() as session:
        deferred = session.get(WarningSchedule, ids["defer"])
        suppressed = session.get(WarningSchedule, ids["suppress"])
        assert deferred.status == "pending"
        assert deferred.next_attempt_at == (sent_at + timedelta(minutes=240)).replace(tzinfo=None)
        assert suppressed.status == "suppressed"
        assert suppressed.payload_json["suppression_reason"] == "minimum_interval"


def test_cancelled_warning_reappearing_with_same_schedule_is_reactivated():
    database = memory_database()
    participant = ParticipantRepository(database).create("WARNING-REAPPEAR")
    warnings = WarningScheduleRepository(database)
    target = NOW + timedelta(minutes=10)
    valid = target + timedelta(minutes=10)
    risk = target + timedelta(minutes=20)
    item = _window("same-episode", target, valid, risk)

    first = _versioned_forecast(database, participant.id, "forecast-a")
    warnings.sync(
        participant.id, DAY, forecast_id=uuid.UUID(first["id"]),
        forecast_version="forecast-a", warnings=[item], now=NOW,
    )
    second = _versioned_forecast(database, participant.id, "forecast-b")
    warnings.sync(
        participant.id, DAY, forecast_id=uuid.UUID(second["id"]),
        forecast_version="forecast-b", warnings=[], now=NOW,
    )
    with database.session() as session:
        assert session.query(WarningSchedule).one().status == "cancelled"

    third = _versioned_forecast(database, participant.id, "forecast-c")
    warnings.sync(
        participant.id, DAY, forecast_id=uuid.UUID(third["id"]),
        forecast_version="forecast-c", warnings=[item], now=NOW,
    )
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        assert row.status == "pending"
        assert row.attempt_count == 0
        assert row.forecast_version == "forecast-c"
        assert row.next_attempt_at <= row.valid_until
    due = warnings.pending(target)
    assert [row["episode_identity"] for row in due] == ["same-episode"]


def _minimum_interval_suppressed_fixture():
    database = memory_database()
    participant = ParticipantRepository(database).create("WARNING-MINIMUM-RECOVERY")
    first = _versioned_forecast(database, participant.id, "forecast-old")
    forecast_id = uuid.UUID(first["id"])
    sent_at = datetime(2030, 1, 15, 8, 0, tzinfo=timezone.utc)
    old_target = datetime(2030, 1, 15, 11, 40, tzinfo=timezone.utc)
    old_valid = datetime(2030, 1, 15, 11, 50, tzinfo=timezone.utc)
    old_risk = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)
    with database.session() as session:
        sent = _warning_row(
            participant.id, forecast_id, "sent-first", target=sent_at,
            valid=sent_at + timedelta(minutes=10), risk=sent_at + timedelta(minutes=20),
            status="sent", sent_at=sent_at,
        )
        candidate = _warning_row(
            participant.id, forecast_id, "minimum-episode", target=old_target,
            valid=old_valid, risk=old_risk,
        )
        candidate.forecast_version = "forecast-old"
        sent.forecast_version = "forecast-old"
        session.add_all([sent, candidate])
        session.flush()
        candidate_id = candidate.id
    repository = WarningScheduleRepository(database)
    assert repository.claim_if_current(candidate_id, now=old_target) is None
    with database.session() as session:
        assert session.get(WarningSchedule, candidate_id).payload_json["suppression_reason"] == "minimum_interval"
    return database, participant, repository, candidate_id


def test_minimum_interval_suppressed_warning_can_use_later_valid_window():
    database, participant, repository, old_id = _minimum_interval_suppressed_fixture()
    latest = _versioned_forecast(database, participant.id, "forecast-new")
    new_target = datetime(2030, 1, 15, 11, 50, tzinfo=timezone.utc)
    new_valid = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)
    new_risk = datetime(2030, 1, 15, 12, 10, tzinfo=timezone.utc)
    diff = repository.sync(
        participant.id, DAY, forecast_id=uuid.UUID(latest["id"]),
        forecast_version="forecast-new", now=new_target,
        warnings=[_window("minimum-episode", new_target, new_valid, new_risk)],
    )
    assert diff["created"] == 1
    with database.session() as session:
        old = session.get(WarningSchedule, old_id)
        rows = session.query(WarningSchedule).filter_by(episode_identity="minimum-episode").all()
        new = next(row for row in rows if row.id != old_id)
        assert old.status == "suppressed"
        assert old.payload_json["suppression_reason"] == "minimum_interval"
        assert new.status == "pending"
        assert new.next_attempt_at == datetime(2030, 1, 15, 12, 0)
        assert new.next_attempt_at <= new.valid_until < new.risk_time


def test_daily_cap_suppressed_warning_never_reactivates_same_day():
    database = memory_database()
    participant = ParticipantRepository(database).create("WARNING-DAILY-TERMINAL")
    first = _versioned_forecast(database, participant.id, "v")
    forecast_id = uuid.UUID(first["id"])
    with database.session() as session:
        session.add_all([
            _warning_row(participant.id, forecast_id, "sent-a", target=NOW, valid=NOW + timedelta(minutes=10), risk=NOW + timedelta(minutes=20), status="sent", sent_at=NOW - timedelta(hours=8)),
            _warning_row(participant.id, forecast_id, "sent-b", target=NOW, valid=NOW + timedelta(minutes=10), risk=NOW + timedelta(minutes=20), status="sent", sent_at=NOW - timedelta(hours=4)),
            _warning_row(participant.id, forecast_id, "daily-episode", target=NOW, valid=NOW + timedelta(hours=1), risk=NOW + timedelta(hours=2)),
        ])
    repository = WarningScheduleRepository(database)
    with database.session() as session:
        candidate_id = session.query(WarningSchedule).filter_by(warning_identity="daily-episode").one().id
    assert repository.claim_if_current(candidate_id, now=NOW) is None
    latest = _versioned_forecast(database, participant.id, "v-new")
    repository.sync(
        participant.id, DAY, forecast_id=uuid.UUID(latest["id"]),
        forecast_version="v-new", now=NOW,
        warnings=[_window("daily-episode", NOW + timedelta(minutes=10), NOW + timedelta(hours=2), NOW + timedelta(hours=3))],
    )
    with database.session() as session:
        rows = session.query(WarningSchedule).filter_by(episode_identity="daily-episode").all()
        assert len(rows) == 1
        assert rows[0].status == "suppressed"
        assert rows[0].payload_json["suppression_reason"] == "daily_cap"


def test_minimum_interval_recovery_still_respects_valid_until():
    database, participant, repository, old_id = _minimum_interval_suppressed_fixture()
    latest = _versioned_forecast(database, participant.id, "forecast-still-too-early")
    repository.sync(
        participant.id, DAY, forecast_id=uuid.UUID(latest["id"]),
        forecast_version="forecast-still-too-early",
        now=datetime(2030, 1, 15, 11, 50, tzinfo=timezone.utc),
        warnings=[_window(
            "minimum-episode",
            datetime(2030, 1, 15, 11, 50, tzinfo=timezone.utc),
            datetime(2030, 1, 15, 11, 59, tzinfo=timezone.utc),
            datetime(2030, 1, 15, 12, 10, tzinfo=timezone.utc),
        )],
    )
    with database.session() as session:
        rows = session.query(WarningSchedule).filter_by(episode_identity="minimum-episode").all()
        assert len(rows) == 1
        assert session.get(WarningSchedule, old_id).status == "suppressed"


def test_expired_audit_row_is_preserved_and_future_window_creates_new_row():
    database = memory_database()
    participant = ParticipantRepository(database).create("POLICY-EXPIRED")
    saved = _forecast(database, participant.id)
    forecast_id = uuid.UUID(saved["id"])
    with database.session() as session:
        old = _warning_row(
            participant.id, forecast_id, "episode", target=NOW - timedelta(hours=2),
            risk=NOW - timedelta(hours=1), valid=NOW - timedelta(hours=1), status="expired",
        )
        session.add(old)
        session.flush()
        old_id = old.id
    repository = WarningScheduleRepository(database)
    diff = repository.sync(
        participant.id, DAY, forecast_id=forecast_id, forecast_version="v", now=NOW,
        warnings=[{
            "episode_identity": "episode", "target_time": NOW,
            "risk_time": NOW + timedelta(hours=2),
            "valid_until": NOW + timedelta(hours=1), "warning_level": "2",
            "payload": {}, "episode_drift_minutes": 15,
        }],
    )
    assert diff["created"] == 1
    with database.session() as session:
        rows = session.query(WarningSchedule).all()
        assert len(rows) == 2
        old = session.get(WarningSchedule, old_id)
        assert old.status == "expired"
        assert old.risk_time == (NOW - timedelta(hours=1)).replace(tzinfo=None)
        assert next(row for row in rows if row.id != old_id).status == "pending"


def test_delivery_unavailable_reschedule_recomputes_recheck_inside_new_window():
    database = memory_database()
    participant = ParticipantRepository(database).create("POLICY-CHANNEL")
    saved = _forecast(database, participant.id)
    forecast_id = uuid.UUID(saved["id"])
    with database.session() as session:
        row = _warning_row(
            participant.id, forecast_id, "channel", target=NOW + timedelta(hours=2),
            risk=NOW + timedelta(hours=5), valid=NOW + timedelta(hours=4),
            status="delivery_unavailable",
        )
        row.next_attempt_at = NOW + timedelta(hours=3)
        session.add(row)
        session.flush()
        row_id = row.id
    repository = WarningScheduleRepository(database)
    repository.sync(
        participant.id, DAY, forecast_id=forecast_id, forecast_version="v", now=NOW,
        warnings=[{
            "episode_identity": "channel", "target_time": NOW,
            "risk_time": NOW + timedelta(hours=1),
            "valid_until": NOW + timedelta(minutes=30), "warning_level": "2",
            "payload": {}, "episode_drift_minutes": 15,
        }],
    )
    with database.session() as session:
        row = session.get(WarningSchedule, row_id)
        assert row.status == "delivery_unavailable"
        assert row.next_attempt_at == NOW.replace(tzinfo=None)
        assert row.next_attempt_at <= row.valid_until < row.risk_time


def test_card_priority_keeps_risk_peak_and_warning_visible_among_many_events():
    events = [
        {"summary": f"日程{i}", "start_time": f"2030-01-15T{8 + i:02d}:00:00+08:00"}
        for i in range(10)
    ]
    analysis = analyze_curve(
        [{"time": "08:00", "stress_0_10": 4}, {"time": "20:00", "stress_0_10": 9}],
        warning_windows=[{"risk_time": "2030-01-15T19:30:00+08:00"}],
        calendar_events=events, timezone_value=ZoneInfo("Asia/Shanghai"),
    )
    card = pressure_curve_card(analysis, image_key="img", local_date=DAY.isoformat())
    text = card["body"]["elements"][2]["content"]
    assert "高风险" in text
    assert "预测峰值" in text
    assert "主动关怀提醒窗口" in text


def test_calendar_mutation_refresh_failure_is_degraded_success_metadata():
    class Coordinator:
        async def ensure_forecast(self, _participant_id, target, _reason, **_kwargs):
            if target.day == 16:
                raise RuntimeError("forecast failed")
            return {"ok": True}

    tools = object.__new__(CareTools)
    tools.forecast_coordinator = Coordinator()
    tools.timezone = ZoneInfo("Asia/Shanghai")
    result = asyncio.run(tools._refresh_calendar_mutation_forecasts(
        "participant", {date(2030, 1, 15), date(2030, 1, 16)}, "mutation"
    ))
    assert result["forecast_refresh"] == "partial"
    assert result["forecast_refresh_degraded"] is True
    assert result["forecast_refreshed_dates"] == ["2030-01-15"]
    assert result["forecast_refresh_errors"] == [
        {"local_date": "2030-01-16", "error_class": "RuntimeError"}
    ]


def test_longitudinal_calibration_requires_seven_days_and_versions_learned_layer():
    database = memory_database()
    participant = ParticipantRepository(database).create("P2-CALIBRATION")
    observations = ObservationRepository(database)
    forecasts = ForecastSnapshotRepository(database)
    learned = LearnedProfileRepository(database)
    for offset in range(7):
        local_day = DAY - timedelta(days=6 - offset)
        forecasts.save(
            participant.id, local_day, calendar_revision=f"c{offset}", semantic_revision="s",
            observation_revision=f"o{offset}", algorithm_version="a",
            forecast_version=f"v{offset}", semantic_status="rules_only",
            semantic_input=[], curve=[{"time": "09:00", "stress_0_10": 5.0}],
            peaks=[], warning_windows=[], output={},
        )
        for hour in (9, 10):
            observed_at = datetime(
                local_day.year, local_day.month, local_day.day, hour,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
            observation_id = observations.add(
                participant.id, "checkin",
                {"stress_0_10": 6.0, "stress_event_since_last": hour == 9},
                observed_at=observed_at,
                source_message_id=f"{local_day}-{hour}",
            )
            with database.session() as session:
                session.get(StateObservation, observation_id).created_at = observed_at
    service = ProfileCalibrationService(observations, forecasts, learned, "Asia/Shanghai")
    result = service.maybe_calibrate(participant.id, through=DAY)
    assert result["status"] == "calibrated"
    assert result["learned_profile"]["version"] == 1
    assert result["learned_profile"]["day_count"] == 7
    assert result["learned_profile"]["parameters"]["S_star_init"] == 52.0
    repeated = service.maybe_calibrate(participant.id, through=DAY)
    assert repeated["status"] == "unchanged"
    effective, layers = layered_profile(
        {"version": 3, "profile": {"model_params": {"S_star_init": 48.0}}},
        result["learned_profile"],
    )
    assert effective["model_params"]["S_star_init"] == 48.0
    assert layers["precedence"] == ["system_defaults", "learned", "explicit"]


def _set_forecast_generated_at(database, forecast_id, value):
    with database.session() as session:
        session.get(ForecastSnapshot, uuid.UUID(forecast_id)).generated_at = value


def _add_observation_at(observations, database, participant_id, *, observed_at, actual, key):
    observation_id = observations.add(
        participant_id, "checkin",
        {"stress_0_10": actual, "stress_event_since_last": False},
        observed_at=observed_at, source_message_id=key,
    )
    with database.session() as session:
        session.get(StateObservation, observation_id).created_at = observed_at
    return observation_id


def _causal_calibration_fixture(*, include_pre_observation=True):
    database = memory_database()
    participant = ParticipantRepository(database).create(f"CAUSAL-{include_pre_observation}")
    observations = ObservationRepository(database)
    forecasts = ForecastSnapshotRepository(database)
    learned = LearnedProfileRepository(database)
    if include_pre_observation:
        before = _versioned_forecast(
            database, participant.id, "before-observation",
            curve=[{"time": "15:00", "stress_0_10": 5.0}],
        )
        _set_forecast_generated_at(
            database, before["id"],
            datetime(2030, 1, 15, 14, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    observation_id = _add_observation_at(
        observations, database, participant.id,
        observed_at=datetime(2030, 1, 15, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        actual=8.0, key="target-observation",
    )
    after = _versioned_forecast(
        database, participant.id, "after-observation",
        curve=[{"time": "15:00", "stress_0_10": 7.8}],
    )
    _set_forecast_generated_at(
        database, after["id"],
        datetime(2030, 1, 15, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    service = ProfileCalibrationService(observations, forecasts, learned, "Asia/Shanghai")
    return database, participant, service, observation_id


def test_calibration_uses_forecast_generated_before_observation():
    _, participant, service, _ = _causal_calibration_fixture()
    samples = service.causal_samples(participant.id, through=DAY)
    assert len(samples) == 1
    assert samples[0]["forecast_version"] == "before-observation"
    assert samples[0]["predicted"] == 5.0


def test_post_observation_forecast_is_not_used_for_calibration():
    _, participant, service, _ = _causal_calibration_fixture(include_pre_observation=False)
    assert service.causal_samples(participant.id, through=DAY) == []


def test_calibration_sample_count_excludes_leaked_samples():
    database, participant, service, _ = _causal_calibration_fixture()
    _add_observation_at(
        service.observations, database, participant.id,
        observed_at=datetime(2030, 1, 15, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        actual=9.0, key="no-earlier-forecast",
    )
    samples = service.causal_samples(participant.id, through=DAY)
    assert len(samples) == 1
    assert samples[0]["actual"] == 8.0


def test_calibration_prediction_error_uses_pre_observation_curve():
    _, participant, service, _ = _causal_calibration_fixture()
    service.MIN_DAYS = 1
    service.MIN_MATCHED_SAMPLES = 1
    result = service.maybe_calibrate(participant.id, through=DAY)
    assert result["status"] == "calibrated"
    assert result["learned_profile"]["sample_count"] == 1
    # Residual is 8 - 5 = 3, so the guarded baseline step reaches +2.
    # Using the leaked 7.8 curve would only move the baseline to 50.4.
    assert result["learned_profile"]["parameters"]["S_star_init"] == 52.0


def test_warning_policy_full_pipeline_closes_candidate_interval_and_daily_cap():
    params = {
        "S_star_init": 50.0, "time_step": 10,
        "alert_thresholds": {
            "yellow_stress": 70, "orange_stress": 80, "red_stress": 88,
            "recovery_stress": 62, "yellow_confirm_minutes": 40,
            "orange_confirm_minutes": 20, "red_confirm_minutes": 10,
            "rearm_minutes": 40,
        },
    }
    rows = []
    for start, stress, count in ((540, 72, 5), (720, 82, 3), (900, 90, 2), (1200, 82, 3)):
        for minute in range(start, start + count * 10, 10):
            rows.append({"time": f"{minute // 60:02d}:{minute % 60:02d}", "S": stress, "V": 72, "F": 0, "state": "DAY_ACTIVE", "delta_S": 1})
        recovery_start = start + count * 10
        for minute in range(recovery_start, recovery_start + 50, 10):
            rows.append({"time": f"{minute // 60:02d}:{minute % 60:02d}", "S": 50, "V": 72, "F": 0, "state": "DAY_ACTIVE", "delta_S": -1})
    rows.sort(key=lambda row: row["time"])
    alerts, _ = AlertMonitor(params).analyze(rows)
    selected = WarningPolicy().select_daily_candidates(alerts)
    assert 1 <= len(selected) <= 2
    selected_minutes = [int(item["time"][:2]) * 60 + int(item["time"][3:5]) for item in selected]
    assert all(
        right - left >= 240
        for left, right in zip(selected_minutes, selected_minutes[1:])
    )

    coordinator = object.__new__(ForecastCoordinator)
    coordinator.timezone = ZoneInfo("Asia/Shanghai")
    coordinator.warning_lead_minutes = 20
    coordinator.warning_late_grace_minutes = 10
    coordinator.warning_episode_drift_minutes = 15
    windows = coordinator._warning_windows(selected, DAY)
    database = memory_database()
    participant = ParticipantRepository(database).create("FULL-WARNING-PIPELINE")
    repository = WarningScheduleRepository(database)
    first_forecast = _versioned_forecast(database, participant.id, "pipeline-v1")
    repository.sync(
        participant.id, DAY, forecast_id=uuid.UUID(first_forecast["id"]),
        forecast_version="pipeline-v1", warnings=windows,
        now=min(item["target_time"] for item in windows) - timedelta(minutes=1),
    )
    with database.session() as session:
        scheduled = session.query(WarningSchedule).order_by(WarningSchedule.target_time).all()
        assert len(scheduled) == len(selected)
        first = scheduled[0]
        first.status = "sent"
        first.sent_at = first.target_time
        if len(scheduled) == 2:
            second_id = scheduled[1].id
            second_target = scheduled[1].target_time.replace(tzinfo=timezone.utc)
    if len(selected) == 2:
        claimed = repository.claim_if_current(second_id, now=second_target)
        assert claimed is not None
        assert repository.finish_claim(
            second_id, claim_token=claimed["claim_token"],
            expected_forecast_version=claimed["forecast_version"], sent=True,
            now=second_target,
        )
    else:
        # The detector is permitted to emit one candidate for this synthetic
        # curve; create a second compliant successful delivery for cap testing.
        with database.session() as session:
            only = session.query(WarningSchedule).one()
            session.add(_warning_row(
                participant.id, only.forecast_id, "second-success",
                target=only.sent_at + timedelta(hours=4),
                valid=only.sent_at + timedelta(hours=4, minutes=10),
                risk=only.sent_at + timedelta(hours=4, minutes=20),
                status="sent", sent_at=only.sent_at + timedelta(hours=4),
            ))
            session.flush()
            session.query(WarningSchedule).filter_by(warning_identity="second-success").one().forecast_version = "pipeline-v1"

    next_forecast = _versioned_forecast(database, participant.id, "pipeline-v2")
    third_target = datetime(2030, 1, 15, 21, 0, tzinfo=timezone.utc)
    repository.sync(
        participant.id, DAY, forecast_id=uuid.UUID(next_forecast["id"]),
        forecast_version="pipeline-v2",
        warnings=[_window("third-episode", third_target, third_target + timedelta(minutes=10), third_target + timedelta(minutes=20))],
        now=third_target,
    )
    with database.session() as session:
        third = session.query(WarningSchedule).filter_by(episode_identity="third-episode").one()
        assert third.status == "suppressed"
        assert third.payload_json["suppression_reason"] == "daily_cap"
