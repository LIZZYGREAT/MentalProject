"""Evidence-based audit and narrowly scoped cleanup for synthetic forecast data."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Iterable
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import CareInterventionEvent, ForecastSnapshot, Participant, WarningSchedule


PLAN_SCHEMA_VERSION = 1
FUTURE_HORIZON_DAYS = 365
ACTIVE_WARNING_STATUSES = {"pending", "claimed", "retry", "scheduled"}
_MARKER_RE = re.compile(
    r"(?:^|[^a-z0-9])(pytest|synthetic|fixture|acceptance[-_ ]?test|"
    r"migration[-_ ]?test|test[-_][a-z0-9_-]+)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


class CleanupPlanError(RuntimeError):
    """Raised when a cleanup plan no longer exactly matches the database."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime, uuid.UUID)):
        return value.isoformat() if not isinstance(value, uuid.UUID) else str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _marker_text(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


def _has_marker(value: Any) -> bool:
    return bool(_MARKER_RE.search(_marker_text(value)))


def _participant_marker(code: str) -> bool:
    folded = code.casefold()
    return folded.startswith(("test-", "test_", "pytest", "synthetic", "fixture")) or _has_marker(code)


def _candidate(
    *,
    table: str,
    row_id: uuid.UUID,
    participant_id: uuid.UUID,
    participant_code: str,
    local_date: date,
    marker_values: Iterable[Any],
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
        "table": table,
        "id": str(row_id),
        "participant_id": str(participant_id),
        "participant_code": participant_code,
        "local_date": local_date.isoformat(),
        "reasons": reasons,
        "eligible_for_cleanup": bool(strong_reasons),
    }


def _plan_digest(plan: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    payload = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise CleanupPlanError("unsupported cleanup plan schema")
    if plan.get("plan_digest") != _plan_digest(plan):
        raise CleanupPlanError("cleanup plan digest does not match its contents")


def _table_summary(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for table in ("forecast_snapshots", "warning_schedules", "care_intervention_events"):
        rows = [row for row in candidates if row["table"] == table]
        dates = [row["local_date"] for row in rows]
        summaries.append(
            {
                "table": table,
                "row_count": len(rows),
                "participant_ids": sorted({row["participant_id"] for row in rows}),
                "participant_codes": sorted({row["participant_code"] for row in rows}),
                "date_range": {"min": min(dates) if dates else None, "max": max(dates) if dates else None},
                "sample_ids": [row["id"] for row in rows[:10]],
                "reasons": sorted({reason for row in rows for reason in row["reasons"]}),
            }
        )
    return summaries


def _invariants(session: Session) -> dict[str, list[dict[str, Any]]]:
    duplicate_valid = session.execute(
        select(
            ForecastSnapshot.participant_id,
            ForecastSnapshot.local_date,
            func.count(ForecastSnapshot.id).label("row_count"),
        )
        .where(ForecastSnapshot.valid.is_(True))
        .group_by(ForecastSnapshot.participant_id, ForecastSnapshot.local_date)
        .having(func.count(ForecastSnapshot.id) > 1)
    ).all()
    invalid_sent = session.scalars(
        select(WarningSchedule).where(
            WarningSchedule.status == "sent",
            (WarningSchedule.authorized_at.is_(None)) | (WarningSchedule.sent_at.is_(None)),
        )
    ).all()
    stale_active = session.execute(
        select(WarningSchedule, ForecastSnapshot)
        .join(ForecastSnapshot, ForecastSnapshot.id == WarningSchedule.forecast_id)
        .where(
            WarningSchedule.status.in_(ACTIVE_WARNING_STATUSES),
            ForecastSnapshot.valid.is_(False),
        )
    ).all()
    return {
        "duplicate_valid_forecasts": [
            {
                "participant_id": str(row.participant_id),
                "local_date": row.local_date.isoformat(),
                "row_count": row.row_count,
            }
            for row in duplicate_valid
        ],
        "sent_warnings_without_authorization_or_sent_time": [
            {"id": str(row.id), "participant_id": str(row.participant_id)} for row in invalid_sent
        ],
        "active_warnings_on_stale_forecasts": [
            {
                "warning_id": str(warning.id),
                "forecast_id": str(forecast.id),
                "participant_id": str(warning.participant_id),
            }
            for warning, forecast in stale_active
        ],
    }


def audit_synthetic_data(engine: Engine, *, today: date | None = None) -> dict[str, Any]:
    """Read only: collect evidence and produce an exact optional cleanup plan."""

    today = today or datetime.now(timezone.utc).date()
    future_cutoff = today + timedelta(days=FUTURE_HORIZON_DAYS)
    candidates: list[dict[str, Any]] = []
    with Session(engine) as session:
        participant_codes = dict(session.execute(select(Participant.id, Participant.participant_code)).all())
        forecasts = session.scalars(select(ForecastSnapshot).order_by(ForecastSnapshot.generated_at)).all()
        warnings = session.scalars(select(WarningSchedule).order_by(WarningSchedule.local_date)).all()
        care_events = session.scalars(select(CareInterventionEvent).order_by(CareInterventionEvent.scheduled_at)).all()

        for row in forecasts:
            item = _candidate(
                table="forecast_snapshots",
                row_id=row.id,
                participant_id=row.participant_id,
                participant_code=participant_codes.get(row.participant_id, "<missing>"),
                local_date=row.local_date,
                marker_values=(
                    row.calendar_revision,
                    row.semantic_revision,
                    row.observation_revision,
                    row.algorithm_version,
                    row.forecast_version,
                    row.semantic_input_json,
                    row.output_json,
                ),
                future_cutoff=future_cutoff,
            )
            if item:
                candidates.append(item)

        eligible_forecasts = {row["id"] for row in candidates if row["table"] == "forecast_snapshots" and row["eligible_for_cleanup"]}
        for row in warnings:
            item = _candidate(
                table="warning_schedules",
                row_id=row.id,
                participant_id=row.participant_id,
                participant_code=participant_codes.get(row.participant_id, "<missing>"),
                local_date=row.local_date,
                marker_values=(row.forecast_version, row.warning_identity, row.episode_identity, row.payload_json),
                future_cutoff=future_cutoff,
            )
            if str(row.forecast_id) in eligible_forecasts:
                item = item or {
                    "table": "warning_schedules",
                    "id": str(row.id),
                    "participant_id": str(row.participant_id),
                    "participant_code": participant_codes.get(row.participant_id, "<missing>"),
                    "local_date": row.local_date.isoformat(),
                    "reasons": [],
                    "eligible_for_cleanup": False,
                }
                item["reasons"].append("source_forecast_confirmed_synthetic")
                item["eligible_for_cleanup"] = True
            if item:
                candidates.append(item)

        eligible_warnings = {row["id"] for row in candidates if row["table"] == "warning_schedules" and row["eligible_for_cleanup"]}
        for row in care_events:
            item = _candidate(
                table="care_intervention_events",
                row_id=row.id,
                participant_id=row.participant_id,
                participant_code=participant_codes.get(row.participant_id, "<missing>"),
                local_date=row.scheduled_at.date(),
                marker_values=(row.forecast_version, row.template_id, row.template_version, row.reason_code, row.context_json),
                future_cutoff=future_cutoff,
            )
            propagated: list[str] = []
            if str(row.source_forecast_id) in eligible_forecasts:
                propagated.append("source_forecast_confirmed_synthetic")
            if str(row.source_warning_id) in eligible_warnings:
                propagated.append("source_warning_confirmed_synthetic")
            if propagated:
                item = item or {
                    "table": "care_intervention_events",
                    "id": str(row.id),
                    "participant_id": str(row.participant_id),
                    "participant_code": participant_codes.get(row.participant_id, "<missing>"),
                    "local_date": row.scheduled_at.date().isoformat(),
                    "reasons": [],
                    "eligible_for_cleanup": False,
                }
                item["reasons"].extend(propagated)
                item["eligible_for_cleanup"] = True
            if item:
                candidates.append(item)

        invariants = _invariants(session)

    plan_rows = [dict(row) for row in candidates if row["eligible_for_cleanup"]]
    plan = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "database_name": engine.url.database,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "future_cutoff": future_cutoff.isoformat(),
        "rows": plan_rows,
        "expected_cleanup_counts": dict(Counter(row["table"] for row in plan_rows)),
    }
    plan["plan_digest"] = _plan_digest(plan)
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": plan["generated_at"],
        "database_name": engine.url.database,
        "future_cutoff": future_cutoff.isoformat(),
        "tables": _table_summary(candidates),
        "candidates": candidates,
        "invariants": invariants,
        "cleanup_plan": plan,
    }


def _ids_by_table(plan: dict[str, Any]) -> dict[str, set[uuid.UUID]]:
    grouped: dict[str, set[uuid.UUID]] = defaultdict(set)
    for row in plan["rows"]:
        if not row.get("eligible_for_cleanup"):
            raise CleanupPlanError("plan contains a row without cleanup evidence")
        grouped[row["table"]].add(uuid.UUID(row["id"]))
    unknown = set(grouped) - {"forecast_snapshots", "warning_schedules", "care_intervention_events"}
    if unknown:
        raise CleanupPlanError(f"unsupported cleanup tables: {sorted(unknown)}")
    return grouped


def cleanup_from_plan(
    engine: Engine,
    plan: dict[str, Any],
    *,
    execute: bool = False,
    backup_confirmed: bool = False,
) -> dict[str, Any]:
    """Delete only planned rows; dry-run rolls back and execute requires backup confirmation."""

    verify_plan(plan)
    if execute and not backup_confirmed:
        raise CleanupPlanError("--execute requires --backup-confirmed")
    if plan.get("database_name") != engine.url.database:
        raise CleanupPlanError("cleanup plan targets a different database")
    ids = _ids_by_table(plan)
    planned_counts = {table: len(values) for table, values in ids.items()}
    if plan.get("expected_cleanup_counts", {}) != planned_counts:
        raise CleanupPlanError("cleanup plan expected counts do not match its row list")
    models = {
        "forecast_snapshots": ForecastSnapshot,
        "warning_schedules": WarningSchedule,
        "care_intervention_events": CareInterventionEvent,
    }
    session = Session(engine)
    try:
        before: dict[str, int] = {}
        for table, model in models.items():
            expected = ids.get(table, set())
            found = set(session.scalars(select(model.id).where(model.id.in_(expected))).all()) if expected else set()
            if found != expected:
                raise CleanupPlanError(f"{table} changed after audit; refusing cleanup")
            before[table] = int(session.scalar(select(func.count()).select_from(model)) or 0)

        forecast_ids = ids.get("forecast_snapshots", set())
        warning_ids = ids.get("warning_schedules", set())
        care_ids = ids.get("care_intervention_events", set())
        unplanned_warnings = session.scalars(
            select(WarningSchedule.id).where(
                WarningSchedule.forecast_id.in_(forecast_ids),
                WarningSchedule.id.not_in(warning_ids),
            )
        ).all() if forecast_ids else []
        unplanned_care = session.scalars(
            select(CareInterventionEvent.id).where(
                ((CareInterventionEvent.source_forecast_id.in_(forecast_ids)) |
                 (CareInterventionEvent.source_warning_id.in_(warning_ids))),
                CareInterventionEvent.id.not_in(care_ids),
            )
        ).all() if forecast_ids or warning_ids else []
        if unplanned_warnings or unplanned_care:
            raise CleanupPlanError("plan omits dependent rows and could cause an unplanned cascade")

        for table in ("care_intervention_events", "warning_schedules", "forecast_snapshots"):
            selected = ids.get(table, set())
            if selected:
                session.execute(delete(models[table]).where(models[table].id.in_(selected)))
        session.flush()
        remaining = {
            table: int(session.scalar(select(func.count()).select_from(model)) or 0)
            for table, model in models.items()
        }
        expected_after = {
            table: count - len(ids.get(table, set()))
            for table, count in before.items()
        }
        if remaining != expected_after:
            raise CleanupPlanError("cleanup row-count mismatch; transaction rolled back")
        for table, selected in ids.items():
            model = models[table]
            if selected and session.scalar(select(func.count()).select_from(model).where(model.id.in_(selected))):
                raise CleanupPlanError(f"planned {table} rows remain after delete")
        if execute:
            session.commit()
        else:
            session.rollback()
        return {
            "mode": "execute" if execute else "dry-run",
            "committed": execute,
            "plan_digest": plan["plan_digest"],
            "planned_counts": planned_counts,
            "before_counts": before,
            "after_counts": remaining,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
