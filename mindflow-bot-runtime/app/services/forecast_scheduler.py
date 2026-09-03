"""Bounded daily forecast preparation and durable warning delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import logging
from typing import Protocol
import uuid
from zoneinfo import ZoneInfo

from app.integrations.feishu.client import FeishuClient, FeishuSendError
from app.integrations.feishu.cards import care_intervention_card
from app.contracts.warning import WarningDeliveryPolicyConfig
from app.repositories import (
    BindingRepository,
    ParticipantRepository,
    RuntimeIncidentRepository,
    WarningScheduleRepository,
)
from app.services.forecast_coordinator import ForecastCoordinator
from app.services.care_outcome_refresh import CareOutcomeRefreshService


class CalibrationService(Protocol):
    def maybe_calibrate(self, participant_id: uuid.UUID, *, through): ...


logger = logging.getLogger(__name__)


class ForecastScheduler:
    def __init__(
        self, *, coordinator: ForecastCoordinator, participants: ParticipantRepository,
        warnings: WarningScheduleRepository, bindings: BindingRepository,
        sender: FeishuClient, timezone_name: str, daily_prepare_local_time: str,
        calendar_sync_interval_seconds: int, warning_poll_interval_seconds: int,
        calendar_oauth_app_id: str,
        forecast_max_concurrency: int = 1, warning_max_attempts: int = 5,
        warning_retry_base_seconds: int = 60, warning_claim_lease_seconds: int = 120,
        warning_delivery_policy: WarningDeliveryPolicyConfig | None = None,
        profile_calibration: CalibrationService | None = None,
        incidents: RuntimeIncidentRepository | None = None,
        care_card_enabled: bool = False,
        care_outcome_refresh: CareOutcomeRefreshService | None = None,
        care_outcome_reconcile_interval_seconds: int = 1200,
    ):
        self.coordinator = coordinator
        self.participants = participants
        self.warnings = warnings
        self.bindings = bindings
        self.sender = sender
        self.calendar_oauth_app_id = calendar_oauth_app_id
        self.timezone = ZoneInfo(timezone_name)
        hour, minute = (int(part) for part in daily_prepare_local_time.split(":"))
        self.daily_time = time(hour, minute)
        self.sync_interval = calendar_sync_interval_seconds
        self.warning_interval = warning_poll_interval_seconds
        self.max_concurrency = max(1, forecast_max_concurrency)
        self.warning_max_attempts = warning_max_attempts
        self.warning_retry_base_seconds = warning_retry_base_seconds
        self.warning_claim_lease_seconds = warning_claim_lease_seconds
        delivery_policy = warning_delivery_policy or (
            warnings.delivery_policy if warnings is not None else None
        )
        if delivery_policy is None:
            raise ValueError("warning delivery policy is required")
        self.warning_delivery_policy = delivery_policy
        if warnings is not None and warnings.delivery_policy is not delivery_policy:
            raise ValueError("warning scheduler and repository must share delivery policy")
        self.profile_calibration = profile_calibration
        self.incidents = incidents
        self.care_card_enabled = bool(care_card_enabled)
        self.care_outcome_refresh = care_outcome_refresh
        self.care_outcome_reconcile_interval = min(
            1800, max(600, int(care_outcome_reconcile_interval_seconds))
        )
        self._stop = asyncio.Event()
        self.started = asyncio.Event()

    def _record_incident(self, **values) -> None:
        if self.incidents is None:
            return
        try:
            self.incidents.record(**values)
        except Exception:
            logger.warning("runtime_incident_persist_failed", exc_info=True)

    async def run_forever(self) -> None:
        self.started.set()
        await asyncio.gather(
            self._forecast_loop(),
            self._warning_loop(),
            self._care_outcome_loop(),
        )

    async def close(self) -> None:
        self._stop.set()

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _forecast_loop(self) -> None:
        last_daily = None
        while not self._stop.is_set():
            try:
                now = datetime.now(self.timezone)
                reason = "periodic_poll"
                if now.time() >= self.daily_time and last_daily != now.date():
                    reason = "daily_prepare"
                    last_daily = now.date()
                participant_ids = await asyncio.to_thread(
                    self.participants.active_calendar_ids,
                    self.calendar_oauth_app_id,
                )
                # Tomorrow depends on today's terminal state.  Finish the
                # entire bounded today phase before any tomorrow job starts.
                await self._run_forecast_phase(
                    participant_ids, now.date(), reason, calibrate=True
                )
                await self._run_forecast_phase(
                    participant_ids,
                    now.date() + timedelta(days=1),
                    "future_periodic_poll",
                    calibrate=False,
                )
            except Exception as exc:
                logger.exception(
                    "forecast_scheduler_iteration_failed error_class=%s message=%s",
                    type(exc).__name__, str(exc)[:160],
                )
                self._record_incident(
                    severity="error",
                    subsystem="forecast_scheduler",
                    event_name="forecast_scheduler_iteration_failed",
                    error_class=type(exc).__name__,
                    summary=str(exc)[:1000],
                )
            await self._wait(self.sync_interval)

    async def _run_forecast_phase(
        self,
        participant_ids: list[uuid.UUID],
        target,
        reason: str,
        *,
        calibrate: bool,
    ) -> None:
        jobs = [(participant_id, target, reason) for participant_id in participant_ids]
        for start in range(0, len(jobs), self.max_concurrency):
            batch = jobs[start : start + self.max_concurrency]
            results = await asyncio.gather(
                *(
                    self.coordinator.ensure_forecast(pid, local_date, why)
                    for pid, local_date, why in batch
                ),
                return_exceptions=True,
            )
            for (pid, local_date, why), result in zip(batch, results):
                if isinstance(result, BaseException):
                    logger.error(
                        "forecast_job_failed participant_id=%s local_date=%s "
                        "reason=%s error_class=%s message=%s",
                        pid,
                        local_date,
                        why,
                        type(result).__name__,
                        str(result)[:160],
                    )
                elif calibrate and self.profile_calibration is not None:
                    try:
                        await asyncio.to_thread(
                            self.profile_calibration.maybe_calibrate,
                            pid,
                            through=local_date,
                        )
                    except Exception as exc:
                        logger.exception(
                            "profile_calibration_failed participant_id=%s "
                            "local_date=%s error_class=%s message=%s",
                            pid,
                            local_date,
                            type(exc).__name__,
                            str(exc)[:160],
                        )

    async def _care_outcome_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.care_outcome_refresh is not None:
                    await self._run_care_outcome_reconcile(
                        datetime.now(timezone.utc)
                    )
            except Exception as exc:
                logger.exception(
                    "care_outcome_reconcile_iteration_failed "
                    "error_class=%s message=%s",
                    type(exc).__name__,
                    str(exc)[:160],
                )
                self._record_incident(
                    severity="error",
                    subsystem="care_outcome_reconcile",
                    event_name="care_outcome_reconcile_iteration_failed",
                    error_class=type(exc).__name__,
                    summary=str(exc)[:1000],
                )
            await self._wait(self.care_outcome_reconcile_interval)

    async def _run_care_outcome_reconcile(
        self, as_of: datetime
    ) -> dict[str, int]:
        if self.care_outcome_refresh is None:
            return {"participants": 0, "failed": 0}
        participant_ids = await asyncio.to_thread(self.participants.active_ids)
        failed = 0
        for participant_id in participant_ids:
            try:
                await asyncio.to_thread(
                    self.care_outcome_refresh.reconcile_recent,
                    participant_id,
                    as_of=as_of,
                    lookback_hours=24,
                )
            except Exception as exc:
                failed += 1
                logger.exception(
                    "care_outcome_reconcile_failed participant_id=%s "
                    "error_class=%s message=%s",
                    participant_id,
                    type(exc).__name__,
                    str(exc)[:160],
                )
                self._record_incident(
                    severity="error",
                    subsystem="care_outcome_reconcile",
                    event_name="care_outcome_reconcile_failed",
                    participant_id=participant_id,
                    error_class=type(exc).__name__,
                    summary=str(exc)[:1000],
                )
        return {"participants": len(participant_ids), "failed": failed}

    async def _warning_loop(self) -> None:
        while not self._stop.is_set():
            try:
                due = await asyncio.to_thread(
                    self.warnings.pending, datetime.now(timezone.utc)
                )
                for item in due:
                    await self._deliver_warning(item)
                await self._recover_delivery_channels()
            except Exception as exc:
                logger.exception(
                    "warning_scheduler_iteration_failed error_class=%s message=%s",
                    type(exc).__name__, str(exc)[:160],
                )
                self._record_incident(
                    severity="error",
                    subsystem="warning_scheduler",
                    event_name="warning_scheduler_iteration_failed",
                    error_class=type(exc).__name__,
                    summary=str(exc)[:1000],
                )
            await self._wait(self.warning_interval)

    async def _recover_delivery_channels(self) -> None:
        now = datetime.now(timezone.utc)
        blocked = await asyncio.to_thread(self.warnings.delivery_unavailable, now)
        for item in blocked:
            binding = await asyncio.to_thread(
                self.bindings.get_for_participant,
                uuid.UUID(item["participant_id"]),
            )
            await asyncio.to_thread(
                self.warnings.reactivate_delivery, uuid.UUID(item["id"]),
                now=now, available=bool(binding and binding.get("chat_id")),
            )

    async def _deliver_warning(self, item: dict) -> None:
        warning_id = uuid.UUID(item["id"])
        now = datetime.now(timezone.utc)
        claimed = await asyncio.to_thread(
            self.warnings.claim_if_current, warning_id, now=now,
            lease_seconds=self.warning_claim_lease_seconds,
        )
        if not claimed:
            return
        claim_token = claimed["claim_token"]
        expected_forecast_version = claimed["forecast_version"]
        binding = await asyncio.to_thread(
            self.bindings.get_for_participant,
            uuid.UUID(claimed["participant_id"]),
        )
        if not binding or not binding.get("chat_id"):
            await asyncio.to_thread(
                self.warnings.block_delivery, warning_id, now=now,
                claim_token=claim_token,
                expected_forecast_version=expected_forecast_version,
                reason="missing_chat_id",
            )
            return
        if not await asyncio.to_thread(
            self.warnings.validate_claim_current, warning_id,
            claim_token=claim_token,
            expected_forecast_version=expected_forecast_version,
            now=datetime.now(timezone.utc),
        ):
            return
        payload = claimed["payload"]
        text = str(
            payload.get("message")
            or payload.get("fallback_message")
            or "预测到临近的高压时段，可以提前安排短暂休息。"
        )
        try:
            plan = dict(payload.get("care_plan") or {})
            care_actions = [
                action
                for action in list(plan.get("actions") or [])
                if action in {
                    "ack",
                    "snooze_30",
                    "mute_today",
                    "helpful",
                    "not_relevant",
                }
            ]
            if self.care_card_enabled and care_actions:
                card = care_intervention_card(
                    intervention_id=str(warning_id),
                    message=text,
                    actions=care_actions,
                )
                await asyncio.to_thread(
                    self.sender.send_card,
                    binding["chat_id"],
                    card,
                    message_uuid=str(warning_id),
                )
            else:
                await asyncio.to_thread(
                    self.sender.send_text, binding["chat_id"], text,
                    message_uuid=str(warning_id),
                )
        except FeishuSendError as exc:
            logger.warning(
                "forecast_warning_send_failed warning_id=%s retryable=%s code=%s",
                warning_id, exc.retryable, exc.code,
            )
            self._record_incident(
                severity="error",
                subsystem="warning_scheduler",
                event_name="forecast_warning_send_failed",
                participant_id=uuid.UUID(claimed["participant_id"]),
                error_code=str(exc.code) if exc.code is not None else None,
                error_class=type(exc).__name__,
                summary="A forecast warning could not be delivered.",
                details={"warning_id": str(warning_id)},
            )
            await asyncio.to_thread(
                self.warnings.finish_claim, warning_id, sent=False,
                claim_token=claim_token,
                expected_forecast_version=expected_forecast_version,
                now=datetime.now(timezone.utc), retryable=exc.retryable,
                error_code=str(exc.code) if exc.code is not None else None,
                error_class=type(exc).__name__, max_attempts=self.warning_max_attempts,
                retry_base_seconds=self.warning_retry_base_seconds,
            )
            return
        except Exception as exc:
            logger.warning(
                "forecast_warning_send_failed warning_id=%s error_class=%s",
                warning_id, type(exc).__name__,
            )
            self._record_incident(
                severity="error",
                subsystem="warning_scheduler",
                event_name="forecast_warning_send_failed",
                participant_id=uuid.UUID(claimed["participant_id"]),
                error_class=type(exc).__name__,
                summary="A forecast warning could not be delivered.",
                details={"warning_id": str(warning_id)},
            )
            await asyncio.to_thread(
                self.warnings.finish_claim, warning_id, sent=False,
                claim_token=claim_token,
                expected_forecast_version=expected_forecast_version,
                now=datetime.now(timezone.utc), retryable=True,
                error_class=type(exc).__name__, max_attempts=self.warning_max_attempts,
                retry_base_seconds=self.warning_retry_base_seconds,
            )
            return
        await asyncio.to_thread(
            self.warnings.finish_claim, warning_id, sent=True,
            claim_token=claim_token,
            expected_forecast_version=expected_forecast_version,
            now=datetime.now(timezone.utc), max_attempts=self.warning_max_attempts,
            retry_base_seconds=self.warning_retry_base_seconds,
        )
