import asyncio
from datetime import datetime, time, timedelta, timezone
import uuid

from app.models import ParticipantCarePreference, WarningSchedule
from app.repositories import ObservationRepository
from tests.test_forecast_hardening import (
    TEST_LOCAL_DATE,
    TEST_NOW,
    build_pipeline,
    event,
)


def _expired_warning():
    database, person, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(person.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.target_time = TEST_NOW - timedelta(minutes=20)
        row.valid_until = TEST_NOW - timedelta(minutes=10)
        row.risk_time = TEST_NOW
        row.next_attempt_at = row.target_time
        warning_id = row.id
    return database, person, warnings, warning_id


def test_expired_unsent_proactive_creates_one_new_late_care_with_new_copy():
    database, _person, warnings, source_id = _expired_warning()

    first = warnings.pending(TEST_NOW)
    second = warnings.pending(TEST_NOW + timedelta(seconds=1))
    with database.session() as session:
        rows = session.query(WarningSchedule).order_by(WarningSchedule.target_time).all()

    assert len(first) == 1
    assert len(second) == 1
    assert len(rows) == 2
    assert rows[0].id == source_id
    assert rows[0].status == "expired"
    late = next(row for row in rows if row.id != source_id)
    assert late.status == "pending"
    assert late.payload_json["delivery_kind"] == "same_day_late_care"
    assert late.payload_json["source_opportunity_id"] == str(source_id)
    assert late.payload_json["care_plan"]["template_id"] == "same-day-late-care-v1"
    assert "刚才" in late.payload_json["message"]
    assert "即将" not in late.payload_json["message"]
    assert "接下来可能进入" not in late.payload_json["message"]


def test_sent_source_and_daily_cap_do_not_create_late_care():
    database, person, warnings, source_id = _expired_warning()
    with database.session() as session:
        source = session.get(WarningSchedule, source_id)
        source.status = "sent"
        source.sent_at = TEST_NOW - timedelta(minutes=5)
    assert warnings.pending(TEST_NOW) == []

    database, person, warnings, source_id = _expired_warning()
    with database.session() as session:
        session.add(
            ParticipantCarePreference(
                participant_id=person.id,
                care_enabled=True,
                warning_enabled=True,
                daily_review_enabled=True,
                max_proactive_care_per_day=0,
            )
        )
    assert warnings.pending(TEST_NOW) == []
    with database.session() as session:
        assert session.query(WarningSchedule).count() == 1
        assert session.get(WarningSchedule, source_id).status == "expired"


def test_late_care_is_not_claimed_during_quiet_hours_and_remains_retryable():
    database, person, warnings, _source_id = _expired_warning()
    local_clock = TEST_NOW.astimezone(warnings.timezone).time().replace(tzinfo=None)
    with database.session() as session:
        session.add(
            ParticipantCarePreference(
                participant_id=person.id,
                care_enabled=True,
                warning_enabled=True,
                daily_review_enabled=True,
                quiet_hours_start=(
                    datetime.combine(TEST_LOCAL_DATE, local_clock)
                    - timedelta(minutes=10)
                ).time(),
                quiet_hours_end=(
                    datetime.combine(TEST_LOCAL_DATE, local_clock)
                    + timedelta(minutes=10)
                ).time(),
            )
        )

    due = warnings.pending(TEST_NOW)
    assert len(due) == 1
    late_id = due[0]["id"]
    assert warnings.claim_if_current(
        uuid.UUID(late_id), now=TEST_NOW
    ) is None
    with database.session() as session:
        late = session.get(WarningSchedule, uuid.UUID(late_id))
        assert late.status == "pending"
        stored_next = late.next_attempt_at.replace(tzinfo=timezone.utc)
        assert stored_next == TEST_NOW + timedelta(minutes=5)


def test_previous_day_missed_warning_is_never_carried_into_next_day():
    database, _person, warnings, source_id = _expired_warning()
    next_local_day = datetime.combine(
        TEST_LOCAL_DATE + timedelta(days=1), time(hour=9), warnings.timezone
    ).astimezone(timezone.utc)

    assert warnings.pending(next_local_day) == []
    with database.session() as session:
        rows = session.query(WarningSchedule).all()
        assert len(rows) == 1
        assert rows[0].id == source_id
        assert rows[0].status == "expired"


def test_fresh_low_stress_high_energy_context_vetoes_late_care():
    database, person, warnings, source_id = _expired_warning()
    ObservationRepository(database).add(
        person.id,
        "check_in",
        {"stress_0_10": 1.0, "energy_0_10": 9.0},
        observed_at=TEST_NOW - timedelta(minutes=1),
    )

    assert warnings.pending(TEST_NOW) == []
    with database.session() as session:
        rows = session.query(WarningSchedule).all()
        assert len(rows) == 1
        assert rows[0].id == source_id
        assert rows[0].status == "expired"


def test_tier_three_late_care_preserves_pause_and_support_severity():
    database, _person, warnings, source_id = _expired_warning()
    with database.session() as session:
        source = session.get(WarningSchedule, source_id)
        source.warning_level = "3"
        payload = dict(source.payload_json)
        payload["care_plan"] = {
            **dict(payload.get("care_plan") or {}),
            "intervention_type": "pause_and_seek_support",
        }
        source.payload_json = payload

    due = warnings.pending(TEST_NOW)
    assert len(due) == 1
    with database.session() as session:
        late = session.get(WarningSchedule, uuid.UUID(due[0]["id"]))
        assert late.warning_level == "3"
        assert (
            late.payload_json["care_plan"]["intervention_type"]
            == "pause_and_seek_support"
        )
        assert late.payload_json["care_plan"]["template_id"] == "pause-and-support-v1"
        assert "暂停" in late.payload_json["message"]
