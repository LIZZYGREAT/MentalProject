"""De-identified, read-only research aggregates with small-cohort suppression."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean, median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import (
    CareInterventionFeedback,
    DailyReviewResponse,
    DailyReviewSchedule,
    ParticipantSlowState,
    ResearchAggregateQueryAudit,
    StateObservation,
)


def _range(
    date_start: str, date_end: str, project_timezone: ZoneInfo,
) -> tuple[date, date, datetime, datetime]:
    start = date.fromisoformat(str(date_start))
    end = date.fromisoformat(str(date_end))
    next_month = (
        date(start.year + 1, 1, 1)
        if start.month == 12 else date(start.year, start.month + 1, 1)
    )
    is_calendar_week = start.weekday() == 0 and end == start + timedelta(days=6)
    is_calendar_month = start.day == 1 and end == next_month - timedelta(days=1)
    if not (is_calendar_week or is_calendar_month):
        raise ValueError("research aggregates require a complete calendar week or month")
    return (
        start,
        end,
        datetime.combine(start, time.min, project_timezone).astimezone(timezone.utc),
        datetime.combine(
            end + timedelta(days=1), time.min, project_timezone
        ).astimezone(timezone.utc),
    )


def _level(value: float) -> str:
    return "high" if value >= 7 else "elevated" if value >= 4 else "low"


class ResearchAggregateService:
    def __init__(
        self, database: Any, *, timezone_name: str,
        minimum_cohort_size: int = 5,
    ) -> None:
        self.database = database
        self.timezone = ZoneInfo(timezone_name)
        self.minimum_cohort_size = max(5, int(minimum_cohort_size))

    def _suppressed(self, cohort_size: int) -> dict[str, Any] | None:
        if cohort_size >= self.minimum_cohort_size:
            return None
        return {
            "suppressed": True,
            "reason": "small_cohort",
            "minimum_cohort_size": self.minimum_cohort_size,
        }

    def _result(
        self, start: date, end: date, cohort: Iterable[Any], *,
        researcher_id: Any = None, tool_name: str | None = None, **summary: Any
    ) -> dict[str, Any]:
        cohort_size = len(set(cohort))
        suppressed = self._suppressed(cohort_size)
        if researcher_id is not None and tool_name:
            with self.database.session() as session:
                session.add(ResearchAggregateQueryAudit(
                    researcher_id=researcher_id, tool_name=tool_name,
                    date_start=start, date_end=end, cohort_size=cohort_size,
                    suppressed=bool(suppressed),
                ))
        if suppressed:
            return {"date_start": start.isoformat(), "date_end": end.isoformat(), **suppressed}
        return {
            "date_start": start.isoformat(), "date_end": end.isoformat(),
            "suppressed": False, "cohort_size": cohort_size, **summary,
        }

    def weekly_stress_summary(self, date_start: str, date_end: str, *, researcher_id: Any = None, tool_name: str | None = None) -> dict[str, Any]:
        start, end, start_at, end_at = _range(
            date_start, date_end, self.timezone
        )
        with self.database.session() as session:
            rows = session.execute(select(StateObservation).where(
                StateObservation.observed_at >= start_at,
                StateObservation.observed_at < end_at,
            )).scalars().all()
        values: list[float] = []
        participants = []
        for row in rows:
            if any(mark in row.observation_type.casefold() for mark in ("safety", "protected")):
                continue
            payload = dict(row.payload_json or {})
            value = payload.get("stress_0_10", payload.get("stress"))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values.append(float(value))
                participants.append(row.participant_id)
        distribution = Counter(_level(value) for value in values)
        return self._result(
            start, end, participants,
            researcher_id=researcher_id, tool_name=tool_name,
            observation_count=len(values),
            mean_stress=round(mean(values), 3) if values else None,
            median_stress=round(median(values), 3) if values else None,
            level_distribution=dict(sorted(distribution.items())),
        )

    def checkin_completion_summary(self, date_start: str, date_end: str, *, researcher_id: Any = None, tool_name: str | None = None) -> dict[str, Any]:
        start, end, _, _ = _range(date_start, date_end, self.timezone)
        with self.database.session() as session:
            schedules = session.execute(select(DailyReviewSchedule).where(
                DailyReviewSchedule.local_date >= start,
                DailyReviewSchedule.local_date <= end,
            )).scalars().all()
            responses = session.execute(select(DailyReviewResponse).where(
                DailyReviewResponse.local_date >= start,
                DailyReviewResponse.local_date <= end,
            )).scalars().all()
        scheduled = {(row.participant_id, row.local_date) for row in schedules}
        completed = {(row.participant_id, row.local_date) for row in responses}
        cohort = [participant_id for participant_id, _ in scheduled | completed]
        denominator = len(scheduled)
        completed_count = len(scheduled & completed)
        return self._result(
            start, end, cohort,
            researcher_id=researcher_id, tool_name=tool_name,
            scheduled_count=denominator,
            completed_count=completed_count,
            completion_rate=(round(completed_count / denominator, 4) if denominator else None),
        )

    def intervention_response_summary(self, date_start: str, date_end: str, *, researcher_id: Any = None, tool_name: str | None = None) -> dict[str, Any]:
        start, end, start_at, end_at = _range(
            date_start, date_end, self.timezone
        )
        with self.database.session() as session:
            rows = session.execute(select(CareInterventionFeedback).where(
                CareInterventionFeedback.submitted_at >= start_at,
                CareInterventionFeedback.submitted_at < end_at,
            )).scalars().all()
        helpfulness = Counter(str(row.helpfulness or "unspecified") for row in rows)
        relevance = Counter(str(row.relevance or "unspecified") for row in rows)
        return self._result(
            start, end, (row.participant_id for row in rows),
            researcher_id=researcher_id, tool_name=tool_name,
            response_count=len(rows),
            helpfulness_distribution=dict(sorted(helpfulness.items())),
            relevance_distribution=dict(sorted(relevance.items())),
        )

    def longitudinal_state_distribution(self, date_start: str, date_end: str, *, researcher_id: Any = None, tool_name: str | None = None) -> dict[str, Any]:
        start, end, start_at, end_at = _range(
            date_start, date_end, self.timezone
        )
        with self.database.session() as session:
            rows = session.execute(select(ParticipantSlowState).where(
                ParticipantSlowState.effective_at >= start_at,
                ParticipantSlowState.effective_at < end_at,
            )).scalars().all()
        stress = Counter(
            _level(float(row.rolling_7d_stress))
            for row in rows if row.rolling_7d_stress is not None
        )
        workload = Counter(
            _level(float(row.rolling_7d_workload))
            for row in rows if row.rolling_7d_workload is not None
        )
        return self._result(
            start, end, (row.participant_id for row in rows),
            researcher_id=researcher_id, tool_name=tool_name,
            state_count=len(rows),
            stress_distribution=dict(sorted(stress.items())),
            workload_distribution=dict(sorted(workload.items())),
        )
