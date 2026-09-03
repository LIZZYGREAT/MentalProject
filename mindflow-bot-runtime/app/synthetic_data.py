"""Evidence-based audit and narrowly scoped cleanup for synthetic forecast data."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Iterable
import uuid

from sqlalchemy import delete, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import (
    CareInterventionEvent, CareInterventionFeedback, CareInterventionOutcome,
    DailyReviewResponse, ForecastCurrentnessEvent, ForecastSnapshot,
    Participant, RetrospectiveCurveSnapshot, WarningSchedule,
)
from app.repositories import WarningScheduleRepository


PLAN_SCHEMA_VERSION = 3
FUTURE_HORIZON_DAYS = 365
OPERATOR_APPROVAL_REASON = "operator_approved_after_audit"
_MARKER_RE = re.compile(
    r"(?:^|[^a-z0-9])(pytest|synthetic|fixture|acceptance[-_ ]?test|"
    r"migration[-_ ]?test|test[-_][a-z0-9_-]+)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


class CleanupPlanError(RuntimeError):
    """Raised when an audit or cleanup plan does not exactly match the database."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _digest(value: dict[str, Any], digest_key: str) -> str:
    unsigned = {key: item for key, item in value.items() if key != digest_key}
    payload = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _marker_text(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


def _has_marker(value: Any) -> bool:
    return bool(_MARKER_RE.search(_marker_text(value)))


def _participant_marker(code: str) -> bool:
    folded = code.casefold()
    return folded.startswith(("test-", "test_", "pytest", "synthetic", "fixture")) or _has_marker(code)


def _candidate(
    *, table: str, row_id: uuid.UUID, participant_id: uuid.UUID,
    participant_code: str, local_date: date, marker_values: Iterable[Any],
    future_cutoff: date,
) -> dict[str, Any] | None:
    reasons: list[str] = []
    strong_reasons: list[str] = []
    if _participant_marker(participant_code):
        strong_reasons.append("participant_code_has_explicit_test_marker")
    if any(_has_marker(value) for value in marker_values):
        strong_reasons.append("row_metadata_has_explicit_test_marker")
    if local_date > future_cutoff:
        reasons.append("far_future_date")
    reasons.extend(strong_reasons)
    if not reasons:
        return None
    return {
        "table": table, "id": str(row_id),
        "participant_id": str(participant_id),
        "participant_code": participant_code, "local_date": local_date.isoformat(),
        "reasons": reasons, "eligible_for_cleanup": bool(strong_reasons),
        "cleanup_blocked": False,
    }


def _apply_operator_approval(
    item: dict[str, Any] | None, approved_ids: set[str], seen: set[str]
) -> dict[str, Any] | None:
    if item is not None and item["id"] in approved_ids:
        item["reasons"].append(OPERATOR_APPROVAL_REASON)
        item["eligible_for_cleanup"] = True
        seen.add(item["id"])
    return item


def verify_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise CleanupPlanError("unsupported cleanup plan schema")
    if plan.get("plan_digest") != _digest(plan, "plan_digest"):
        raise CleanupPlanError("cleanup plan digest does not match its contents")


def verify_audit_report(report: dict[str, Any]) -> None:
    if report.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise CleanupPlanError("unsupported audit report schema")
    if report.get("audit_digest") != _digest(report, "audit_digest"):
        raise CleanupPlanError("audit report digest does not match its contents")


def _table_summary(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for table in ("forecast_snapshots", "warning_schedules", "care_intervention_events"):
        rows = [row for row in candidates if row["table"] == table]
        dates = [row["local_date"] for row in rows]
        summaries.append({
            "table": table, "row_count": len(rows),
            "participant_ids": sorted({row["participant_id"] for row in rows}),
            "participant_codes": sorted({row["participant_code"] for row in rows}),
            "date_range": {"min": min(dates) if dates else None, "max": max(dates) if dates else None},
            "sample_ids": [row["id"] for row in rows[:10]],
            "reasons": sorted({reason for row in rows for reason in row["reasons"]}),
        })
    return summaries


def _invariants(session: Session) -> dict[str, list[dict[str, Any]]]:
    duplicate_valid = session.execute(
        select(ForecastSnapshot.participant_id, ForecastSnapshot.local_date,
               func.count(ForecastSnapshot.id).label("row_count"))
        .where(ForecastSnapshot.valid.is_(True))
        .group_by(ForecastSnapshot.participant_id, ForecastSnapshot.local_date)
        .having(func.count(ForecastSnapshot.id) > 1)
    ).all()
    invalid_sent = session.scalars(
        select(WarningSchedule).where(
            WarningSchedule.status == "sent",
            or_(WarningSchedule.authorized_at.is_(None), WarningSchedule.sent_at.is_(None),
                WarningSchedule.sent_at < WarningSchedule.authorized_at),
        )
    ).all()
    stale_active = session.execute(
        select(WarningSchedule, ForecastSnapshot)
        .outerjoin(ForecastSnapshot, ForecastSnapshot.id == WarningSchedule.forecast_id)
        .where(
            WarningSchedule.status.in_(WarningScheduleRepository.ACTIVE),
            or_(ForecastSnapshot.id.is_(None), ForecastSnapshot.valid.is_not(True),
                WarningSchedule.forecast_version != ForecastSnapshot.forecast_version),
        )
    ).all()
    return {
        "duplicate_valid_forecasts": [{
            "participant_id": str(row.participant_id),
            "local_date": row.local_date.isoformat(), "row_count": row.row_count,
        } for row in duplicate_valid],
        "sent_warnings_without_authorization_or_sent_time": [{
            "id": str(row.id), "participant_id": str(row.participant_id),
            "authorized_at": _jsonable(row.authorized_at), "sent_at": _jsonable(row.sent_at),
        } for row in invalid_sent],
        "active_warnings_on_stale_forecasts": [{
            "warning_id": str(warning.id), "forecast_id": str(warning.forecast_id),
            "participant_id": str(warning.participant_id),
            "warning_forecast_version": warning.forecast_version,
            "forecast_version": forecast.forecast_version if forecast else None,
            "forecast_valid": forecast.valid if forecast else None,
        } for warning, forecast in stale_active],
    }


def _impact(*, table: str, row_id: Any, relation: str, planned_action: str) -> dict[str, Any]:
    return {"table": table, "id": str(row_id), "relation": relation, "planned_action": planned_action}


def audit_synthetic_data(
    engine: Engine, *, today: date | None = None,
    operator_approved_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Read only: collect evidence and produce an exact optional cleanup plan."""

    today = today or datetime.now(timezone.utc).date()
    future_cutoff = today + timedelta(days=FUTURE_HORIZON_DAYS)
    approved_ids = {str(value) for value in operator_approved_ids}
    seen_approvals: set[str] = set()
    candidates: list[dict[str, Any]] = []
    impacts = {"cascade_delete": [], "set_null": [], "restrict_blockers": []}
    with Session(engine) as session:
        participant_codes = dict(session.execute(select(Participant.id, Participant.participant_code)).all())
        forecasts = session.scalars(select(ForecastSnapshot).order_by(ForecastSnapshot.generated_at)).all()
        warnings = session.scalars(select(WarningSchedule).order_by(WarningSchedule.local_date)).all()
        care_events = session.scalars(select(CareInterventionEvent).order_by(CareInterventionEvent.scheduled_at)).all()

        for row in forecasts:
            item = _candidate(
                table="forecast_snapshots", row_id=row.id, participant_id=row.participant_id,
                participant_code=participant_codes.get(row.participant_id, "<missing>"),
                local_date=row.local_date,
                marker_values=(row.calendar_revision, row.semantic_revision, row.observation_revision,
                               row.algorithm_version, row.forecast_version, row.semantic_input_json, row.output_json),
                future_cutoff=future_cutoff,
            )
            item = _apply_operator_approval(item, approved_ids, seen_approvals)
            if item:
                candidates.append(item)

        eligible_forecasts = {row["id"] for row in candidates
                              if row["table"] == "forecast_snapshots" and row["eligible_for_cleanup"]}
        for row in warnings:
            item = _candidate(
                table="warning_schedules", row_id=row.id, participant_id=row.participant_id,
                participant_code=participant_codes.get(row.participant_id, "<missing>"),
                local_date=row.local_date,
                marker_values=(row.forecast_version, row.warning_identity, row.episode_identity, row.payload_json),
                future_cutoff=future_cutoff,
            )
            item = _apply_operator_approval(item, approved_ids, seen_approvals)
            if str(row.forecast_id) in eligible_forecasts:
                item = item or {
                    "table": "warning_schedules", "id": str(row.id),
                    "participant_id": str(row.participant_id),
                    "participant_code": participant_codes.get(row.participant_id, "<missing>"),
                    "local_date": row.local_date.isoformat(), "reasons": [],
                    "eligible_for_cleanup": False, "cleanup_blocked": False,
                }
                item["reasons"].append("source_forecast_confirmed_synthetic")
                item["eligible_for_cleanup"] = True
            if item:
                candidates.append(item)

        eligible_warnings = {row["id"] for row in candidates
                             if row["table"] == "warning_schedules" and row["eligible_for_cleanup"]}
        for row in care_events:
            item = _candidate(
                table="care_intervention_events", row_id=row.id, participant_id=row.participant_id,
                participant_code=participant_codes.get(row.participant_id, "<missing>"),
                local_date=row.scheduled_at.date(),
                marker_values=(row.forecast_version, row.template_id, row.template_version,
                               row.reason_code, row.context_json), future_cutoff=future_cutoff,
            )
            item = _apply_operator_approval(item, approved_ids, seen_approvals)
            propagated: list[str] = []
            if str(row.source_forecast_id) in eligible_forecasts:
                propagated.append("source_forecast_confirmed_synthetic")
            if str(row.source_warning_id) in eligible_warnings:
                propagated.append("source_warning_confirmed_synthetic")
            if propagated:
                item = item or {
                    "table": "care_intervention_events", "id": str(row.id),
                    "participant_id": str(row.participant_id),
                    "participant_code": participant_codes.get(row.participant_id, "<missing>"),
                    "local_date": row.scheduled_at.date().isoformat(), "reasons": [],
                    "eligible_for_cleanup": False, "cleanup_blocked": False,
                }
                item["reasons"].extend(propagated)
                item["eligible_for_cleanup"] = True
            if item:
                candidates.append(item)

        missing = approved_ids - seen_approvals
        if missing:
            raise CleanupPlanError(f"operator approval IDs are not current audit candidates: {sorted(missing)}")

        forecast_ids = {uuid.UUID(row["id"]) for row in candidates
                        if row["table"] == "forecast_snapshots" and row["eligible_for_cleanup"]}
        warning_ids = {uuid.UUID(row["id"]) for row in candidates
                       if row["table"] == "warning_schedules" and row["eligible_for_cleanup"]}
        care_ids = {uuid.UUID(row["id"]) for row in candidates
                    if row["table"] == "care_intervention_events" and row["eligible_for_cleanup"]}

        restrict_responses = session.scalars(select(DailyReviewResponse).where(
            DailyReviewResponse.causal_source_forecast_id.in_(forecast_ids)
        )).all() if forecast_ids else []
        restrict_retrospectives = session.scalars(select(RetrospectiveCurveSnapshot).where(
            RetrospectiveCurveSnapshot.source_forecast_id.in_(forecast_ids)
        )).all() if forecast_ids else []
        blocked_forecasts = {row.causal_source_forecast_id for row in restrict_responses} | {
            row.source_forecast_id for row in restrict_retrospectives
        }
        for row in restrict_responses:
            impacts["restrict_blockers"].append(_impact(
                table="daily_review_responses", row_id=row.id,
                relation="causal_source_forecast_id -> forecast_snapshots.id",
                planned_action="block_forecast_cleanup"))
        for row in restrict_retrospectives:
            impacts["restrict_blockers"].append(_impact(
                table="retrospective_curve_snapshots", row_id=row.id,
                relation="source_forecast_id -> forecast_snapshots.id",
                planned_action="block_forecast_cleanup"))

        set_null_warnings = session.scalars(select(WarningSchedule).where(
            WarningSchedule.snoozed_from_intervention_id.in_(care_ids),
            WarningSchedule.id.not_in(warning_ids),
        )).all() if care_ids else []
        blocked_care = {row.snoozed_from_intervention_id for row in set_null_warnings}
        for row in set_null_warnings:
            impacts["set_null"].append(_impact(
                table="warning_schedules", row_id=row.id,
                relation="snoozed_from_intervention_id -> care_intervention_events.id",
                planned_action="block_care_cleanup"))

        for item in candidates:
            row_id = uuid.UUID(item["id"])
            if item["table"] == "forecast_snapshots" and row_id in blocked_forecasts:
                item["eligible_for_cleanup"] = False
                item["cleanup_blocked"] = True
                item["reasons"].append("restrict_dependency_blocks_cleanup")
            if item["table"] == "care_intervention_events" and row_id in blocked_care:
                item["eligible_for_cleanup"] = False
                item["cleanup_blocked"] = True
                item["reasons"].append("set_null_dependency_blocks_cleanup")

        final_forecasts = {uuid.UUID(row["id"]) for row in candidates
                           if row["table"] == "forecast_snapshots" and row["eligible_for_cleanup"]}
        final_care = {uuid.UUID(row["id"]) for row in candidates
                      if row["table"] == "care_intervention_events" and row["eligible_for_cleanup"]}
        currentness_rows = session.scalars(select(ForecastCurrentnessEvent).where(
            ForecastCurrentnessEvent.forecast_id.in_(final_forecasts)
        )).all() if final_forecasts else []
        feedback_rows = session.scalars(select(CareInterventionFeedback).where(
            CareInterventionFeedback.intervention_id.in_(final_care)
        )).all() if final_care else []
        outcome_rows = session.scalars(select(CareInterventionOutcome).where(
            CareInterventionOutcome.intervention_id.in_(final_care)
        )).all() if final_care else []
        for row in currentness_rows:
            impacts["cascade_delete"].append(_impact(
                table="forecast_currentness_events", row_id=row.id,
                relation="forecast_id -> forecast_snapshots.id", planned_action="explicit_delete"))
        for row in feedback_rows:
            impacts["cascade_delete"].append(_impact(
                table="care_intervention_feedback", row_id=row.id,
                relation="intervention_id -> care_intervention_events.id", planned_action="explicit_delete"))
        for row in outcome_rows:
            impacts["cascade_delete"].append(_impact(
                table="care_intervention_outcomes", row_id=row.intervention_id,
                relation="intervention_id -> care_intervention_events.id", planned_action="explicit_delete"))
        invariants = _invariants(session)

    root_rows = [dict(row) for row in candidates if row["eligible_for_cleanup"]]
    dependent_rows = [{
        "table": impact["table"], "id": impact["id"],
        "reasons": ["explicit_cascade_dependency"], "relation": impact["relation"],
        "eligible_for_cleanup": True, "cleanup_blocked": False,
    } for impact in impacts["cascade_delete"]]
    plan_rows = root_rows + dependent_rows
    generated_at = datetime.now(timezone.utc).isoformat()
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION, "database_name": engine.url.database,
        "generated_at": generated_at, "future_cutoff": future_cutoff.isoformat(),
        "rows": plan_rows, "dependent_impacts": impacts,
        "expected_cleanup_counts": dict(Counter(row["table"] for row in plan_rows)),
    }
    plan["plan_digest"] = _digest(plan, "plan_digest")
    report = {
        "schema_version": PLAN_SCHEMA_VERSION, "generated_at": generated_at,
        "database_name": engine.url.database, "future_cutoff": future_cutoff.isoformat(),
        "tables": _table_summary(candidates), "candidates": candidates,
        "dependent_impacts": impacts, "invariants": invariants, "cleanup_plan": plan,
    }
    report["audit_digest"] = _digest(report, "audit_digest")
    return report


def approve_cleanup_candidates(
    engine: Engine, audit_report: dict[str, Any], candidate_ids: Iterable[str]
) -> dict[str, Any]:
    """Re-audit the database and approve only IDs present in an intact report."""

    verify_audit_report(audit_report)
    if audit_report.get("database_name") != engine.url.database:
        raise CleanupPlanError("audit report targets a different database")
    approvals = {str(value) for value in candidate_ids}
    if not approvals:
        raise CleanupPlanError("at least one --approve-id is required")
    original = {row["id"]: row for row in audit_report.get("candidates", [])}
    unknown = approvals - set(original)
    if unknown:
        raise CleanupPlanError(f"approval IDs are not in the original audit: {sorted(unknown)}")
    original_today = date.fromisoformat(audit_report["future_cutoff"]) - timedelta(days=FUTURE_HORIZON_DAYS)
    refreshed = audit_synthetic_data(engine, today=original_today, operator_approved_ids=approvals)
    refreshed_by_id = {row["id"]: row for row in refreshed["candidates"]}
    for row_id in approvals:
        before, after = original[row_id], refreshed_by_id.get(row_id)
        stable_fields = ("table", "participant_id", "participant_code", "local_date")
        if after is None or any(before.get(field) != after.get(field) for field in stable_fields):
            raise CleanupPlanError(f"candidate {row_id} changed after audit")
        before_evidence = set(before.get("reasons", []))
        after_evidence = set(after.get("reasons", [])) - {OPERATOR_APPROVAL_REASON}
        if not before_evidence <= after_evidence:
            raise CleanupPlanError(f"candidate {row_id} evidence changed after audit")
    return refreshed["cleanup_plan"]


_MODELS = {
    "care_intervention_feedback": CareInterventionFeedback,
    "care_intervention_outcomes": CareInterventionOutcome,
    "forecast_currentness_events": ForecastCurrentnessEvent,
    "care_intervention_events": CareInterventionEvent,
    "warning_schedules": WarningSchedule,
    "forecast_snapshots": ForecastSnapshot,
}
_DELETE_ORDER = tuple(_MODELS)
_PRIMARY_KEYS = {
    "care_intervention_feedback": CareInterventionFeedback.id,
    "care_intervention_outcomes": CareInterventionOutcome.intervention_id,
    "forecast_currentness_events": ForecastCurrentnessEvent.id,
    "care_intervention_events": CareInterventionEvent.id,
    "warning_schedules": WarningSchedule.id,
    "forecast_snapshots": ForecastSnapshot.id,
}
_INTEGER_ID_TABLES = {"forecast_currentness_events"}


def _ids_by_table(plan: dict[str, Any]) -> dict[str, set[Any]]:
    grouped: dict[str, set[Any]] = defaultdict(set)
    for row in plan["rows"]:
        if not row.get("eligible_for_cleanup") or row.get("cleanup_blocked"):
            raise CleanupPlanError("plan contains a row without cleanup evidence")
        table = row["table"]
        if table not in _MODELS:
            raise CleanupPlanError(f"unsupported cleanup table: {table}")
        row_id = int(row["id"]) if table in _INTEGER_ID_TABLES else uuid.UUID(row["id"])
        grouped[table].add(row_id)
    return grouped


def cleanup_from_plan(
    engine: Engine, plan: dict[str, Any], *, execute: bool = False,
    backup_confirmed: bool = False,
) -> dict[str, Any]:
    """Delete every planned row explicitly; dry-run always rolls back."""

    verify_plan(plan)
    if execute and not backup_confirmed:
        raise CleanupPlanError("--execute requires --backup-confirmed")
    if plan.get("database_name") != engine.url.database:
        raise CleanupPlanError("cleanup plan targets a different database")
    ids = _ids_by_table(plan)
    planned_counts = {table: len(values) for table, values in ids.items()}
    if plan.get("expected_cleanup_counts", {}) != planned_counts:
        raise CleanupPlanError("cleanup plan expected counts do not match its row list")
    if plan.get("dependent_impacts", {}).get("set_null"):
        raise CleanupPlanError("cleanup plan contains an unapproved SET NULL side effect")
    if plan.get("dependent_impacts", {}).get("restrict_blockers"):
        raise CleanupPlanError("cleanup plan contains a RESTRICT blocker")

    session = Session(engine)
    try:
        before: dict[str, int] = {}
        for table, model in _MODELS.items():
            expected = ids.get(table, set())
            primary_key = _PRIMARY_KEYS[table]
            found = set(session.scalars(select(primary_key).where(primary_key.in_(expected))).all()) if expected else set()
            if found != expected:
                raise CleanupPlanError(f"{table} changed after audit; refusing cleanup")
            before[table] = int(session.scalar(select(func.count()).select_from(model)) or 0)

        forecast_ids = ids.get("forecast_snapshots", set())
        warning_ids = ids.get("warning_schedules", set())
        care_ids = ids.get("care_intervention_events", set())
        currentness_ids = ids.get("forecast_currentness_events", set())
        feedback_ids = ids.get("care_intervention_feedback", set())
        outcome_ids = ids.get("care_intervention_outcomes", set())
        actual_currentness = set(session.scalars(select(ForecastCurrentnessEvent.id).where(
            ForecastCurrentnessEvent.forecast_id.in_(forecast_ids)
        )).all()) if forecast_ids else set()
        actual_feedback = set(session.scalars(select(CareInterventionFeedback.id).where(
            CareInterventionFeedback.intervention_id.in_(care_ids)
        )).all()) if care_ids else set()
        actual_outcomes = set(session.scalars(select(CareInterventionOutcome.intervention_id).where(
            CareInterventionOutcome.intervention_id.in_(care_ids)
        )).all()) if care_ids else set()
        if (actual_currentness != currentness_ids or actual_feedback != feedback_ids
                or actual_outcomes != outcome_ids):
            raise CleanupPlanError("CASCADE dependencies changed after audit; refusing cleanup")

        unplanned_warnings = session.scalars(select(WarningSchedule.id).where(
            WarningSchedule.forecast_id.in_(forecast_ids), WarningSchedule.id.not_in(warning_ids)
        )).all() if forecast_ids else []
        unplanned_care = session.scalars(select(CareInterventionEvent.id).where(
            or_(CareInterventionEvent.source_forecast_id.in_(forecast_ids),
                CareInterventionEvent.source_warning_id.in_(warning_ids)),
            CareInterventionEvent.id.not_in(care_ids),
        )).all() if forecast_ids or warning_ids else []
        set_null_warnings = session.scalars(select(WarningSchedule.id).where(
            WarningSchedule.snoozed_from_intervention_id.in_(care_ids),
            WarningSchedule.id.not_in(warning_ids),
        )).all() if care_ids else []
        restrict_responses = session.scalars(select(DailyReviewResponse.id).where(
            DailyReviewResponse.causal_source_forecast_id.in_(forecast_ids)
        )).all() if forecast_ids else []
        restrict_retrospectives = session.scalars(select(RetrospectiveCurveSnapshot.id).where(
            RetrospectiveCurveSnapshot.source_forecast_id.in_(forecast_ids)
        )).all() if forecast_ids else []
        if unplanned_warnings or unplanned_care:
            raise CleanupPlanError("plan omits dependent rows and could cause an unplanned cascade")
        if set_null_warnings:
            raise CleanupPlanError("cleanup would modify an unplanned warning through SET NULL")
        if restrict_responses or restrict_retrospectives:
            raise CleanupPlanError("cleanup is blocked by a current RESTRICT dependency")

        for table in _DELETE_ORDER:
            selected = ids.get(table, set())
            if selected:
                session.execute(delete(_MODELS[table]).where(_PRIMARY_KEYS[table].in_(selected)))
        session.flush()
        remaining = {table: int(session.scalar(select(func.count()).select_from(model)) or 0)
                     for table, model in _MODELS.items()}
        expected_after = {table: count - len(ids.get(table, set())) for table, count in before.items()}
        if remaining != expected_after:
            raise CleanupPlanError("cleanup row-count mismatch; transaction rolled back")
        for table, selected in ids.items():
            model = _MODELS[table]
            if selected and session.scalar(select(func.count()).select_from(model).where(
                _PRIMARY_KEYS[table].in_(selected)
            )):
                raise CleanupPlanError(f"planned {table} rows remain after delete")
        if execute:
            session.commit()
        else:
            session.rollback()
        return {
            "mode": "execute" if execute else "dry-run", "committed": execute,
            "plan_digest": plan["plan_digest"], "planned_counts": planned_counts,
            "before_counts": before, "after_counts": remaining,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
