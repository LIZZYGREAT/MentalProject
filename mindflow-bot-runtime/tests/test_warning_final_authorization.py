import asyncio
from datetime import timedelta

from app.models import WarningSchedule
from tests.test_forecast_hardening import (
    TEST_LOCAL_DATE,
    TEST_NOW,
    build_pipeline,
    event,
)


def test_sent_transition_requires_repository_final_authorization():
    database, person, _, _, _, warnings, coordinator = build_pipeline([event()])

    async def prepare():
        await coordinator.ensure_forecast(person.id, TEST_LOCAL_DATE, "daily_prepare")

    asyncio.run(prepare())
    with database.session() as session:
        row = session.query(WarningSchedule).one()
        row.target_time = TEST_NOW - timedelta(seconds=1)
        row.next_attempt_at = row.target_time
        row.valid_until = TEST_NOW + timedelta(minutes=10)
        row.risk_time = TEST_NOW + timedelta(minutes=20)
        warning_id = row.id
        forecast_version = row.forecast_version

    claimed = warnings.claim_if_current(warning_id, now=TEST_NOW)
    assert claimed is not None
    assert warnings.finish_claim(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=forecast_version,
        sent=True,
        now=TEST_NOW + timedelta(seconds=1),
    ) is False
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "claimed"
        assert row.sent_at is None
        assert row.authorized_at is None

    assert warnings.validate_claim_current(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=forecast_version,
        now=TEST_NOW + timedelta(seconds=2),
    )
    assert warnings.finish_claim(
        warning_id,
        claim_token=claimed["claim_token"],
        expected_forecast_version=forecast_version,
        sent=True,
        now=TEST_NOW + timedelta(seconds=3),
    )
    with database.session() as session:
        row = session.get(WarningSchedule, warning_id)
        assert row.status == "sent"
        assert row.sent_at >= row.authorized_at
