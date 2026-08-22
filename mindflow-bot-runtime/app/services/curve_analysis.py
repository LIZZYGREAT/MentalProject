"""One authoritative analysis of a full-day forecast curve."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, tzinfo
from typing import Any


MEDIUM_RISK = 6.0
HIGH_RISK = 8.0


@dataclass(frozen=True)
class ForecastModelContext:
    family: str
    variant: str
    active_states: tuple[str, ...]
    stress_baseline: float
    stress_threshold: float
    vitality_baseline: float
    energy_critical: float

    @property
    def has_dynamic_vitality(self) -> bool:
        return "V" in self.active_states

    @property
    def has_fatigue_state(self) -> bool:
        return "F" in self.active_states


def forecast_model_context(output: dict[str, Any] | None) -> ForecastModelContext:
    """Normalize persisted model metadata for cards and plots.

    Missing metadata is treated as the conservative production M0 baseline;
    presentation code must never infer a richer model from cosmetic curve data.
    """

    value = output if isinstance(output, dict) else {}
    family = str(value.get("model_family") or "stress-ctssm.m0")
    variant = str(value.get("model_variant") or "m0").strip().lower()
    raw_states = value.get("active_states")
    active_states = (
        tuple(str(item).upper() for item in raw_states)
        if isinstance(raw_states, (list, tuple)) and raw_states
        else ("S",)
    )

    def number(key: str, default: float) -> float:
        try:
            return float(value.get(key, default))
        except (TypeError, ValueError):
            return default

    return ForecastModelContext(
        family=family,
        variant=variant,
        active_states=active_states,
        stress_baseline=max(0.0, min(10.0, number("stress_baseline_0_10", 5.0))),
        stress_threshold=max(0.0, min(10.0, number("stress_threshold_0_10", 7.0))),
        vitality_baseline=max(0.0, min(10.0, number("vitality_baseline_0_10", 7.2))),
        energy_critical=max(0.0, min(10.0, number("energy_critical_0_10", 2.5))),
    )


@dataclass(frozen=True)
class CurvePoint:
    time: str
    minute: int
    stress: float
    vitality: float | None
    confidence: float | None
    penalty: float
    stress_equilibrium: float | None
    stress_interval_lower: float | None
    stress_interval_upper: float | None
    event_stress_input: float
    anticipatory_input: float
    post_event_input: float


@dataclass(frozen=True)
class KeyNode:
    time: str
    type: str
    text: str


@dataclass(frozen=True)
class CurveAnalysis:
    point_count: int
    peak_stress: float
    peak_stress_time: str
    current_stress: float
    current_vitality: float | None
    current_time: str
    first_medium_risk_time: str | None
    first_high_risk_time: str | None
    warning_windows: tuple[dict[str, Any], ...]
    recovery_points: tuple[KeyNode, ...]
    important_calendar_events: tuple[dict[str, Any], ...]
    key_nodes: tuple[KeyNode, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _minute_and_label(
    value: Any, timezone_value: tzinfo | None = None
) -> tuple[int, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "T" in text or (" " in text and len(text.split(" ", 1)[0]) >= 8):
        try:
            parsed_datetime = datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            )
            if timezone_value is not None and parsed_datetime.tzinfo is not None:
                parsed_datetime = parsed_datetime.astimezone(timezone_value)
            return (
                parsed_datetime.hour * 60 + parsed_datetime.minute,
                parsed_datetime.strftime("%H:%M"),
            )
        except ValueError:
            pass
    candidate = text
    if "T" in candidate:
        candidate = candidate.split("T", 1)[1]
    elif " " in candidate and len(candidate.split(" ", 1)[0]) >= 8:
        candidate = candidate.split(" ", 1)[1]
    candidate = candidate[:5]
    try:
        hour, minute = (int(part) for part in candidate.split(":"))
    except (TypeError, ValueError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute, f"{hour:02d}:{minute:02d}"


def curve_points(
    curve: list[dict[str, Any]], *, timezone_value: tzinfo | None = None
) -> list[CurvePoint]:
    points: list[CurvePoint] = []
    for raw in curve:
        parsed = _minute_and_label(raw.get("time"), timezone_value)
        if parsed is None:
            continue
        try:
            stress = max(0.0, min(10.0, float(raw.get("stress_0_10"))))
        except (TypeError, ValueError):
            continue
        try:
            vitality = max(0.0, min(10.0, float(raw.get("vitality_0_10"))))
        except (TypeError, ValueError):
            vitality = None
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence_0_1"))))
        except (TypeError, ValueError):
            confidence = None
        try:
            penalty = max(0.0, float(raw.get("continuous_load_penalty") or 0.0))
        except (TypeError, ValueError):
            penalty = 0.0
        try:
            stress_equilibrium = max(
                0.0, min(10.0, float(raw.get("stress_equilibrium_0_10")))
            )
        except (TypeError, ValueError):
            stress_equilibrium = None
        interval = raw.get("stress_interval_90_0_10")
        interval = interval if isinstance(interval, dict) else {}
        try:
            interval_lower = max(0.0, min(10.0, float(interval.get("lower"))))
            interval_upper = max(0.0, min(10.0, float(interval.get("upper"))))
        except (TypeError, ValueError):
            interval_lower = None
            interval_upper = None

        def unit(key: str) -> float:
            try:
                return max(0.0, min(1.0, float(raw.get(key) or 0.0)))
            except (TypeError, ValueError):
                return 0.0

        points.append(
            CurvePoint(
                parsed[1],
                parsed[0],
                stress,
                vitality,
                confidence,
                penalty,
                stress_equilibrium,
                interval_lower,
                interval_upper,
                unit("event_stress_input"),
                unit("anticipatory_input"),
                unit("post_event_input"),
            )
        )
    points.sort(key=lambda point: point.minute)
    if not points:
        raise ValueError("pressure curve has no plottable points")
    return points


def _event_time(
    event: dict[str, Any], timezone_value: tzinfo | None
) -> tuple[int, str] | None:
    return _minute_and_label(
        event.get("start_time") or event.get("time"), timezone_value
    )


def analyze_curve(
    curve: list[dict[str, Any]],
    *,
    warning_windows: list[dict[str, Any]] | None = None,
    calendar_events: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    timezone_value: tzinfo | None = None,
) -> CurveAnalysis:
    """Analyze every valid point; no sampling or prefix truncation is allowed."""

    points = curve_points(curve, timezone_value=timezone_value)
    peak = max(points, key=lambda point: point.stress)
    now_value = now or datetime.now().astimezone()
    current_minute = now_value.hour * 60 + now_value.minute
    current = min(points, key=lambda point: abs(point.minute - current_minute))

    first_medium = next((p for p in points if p.stress >= MEDIUM_RISK), None)
    first_high = next((p for p in points if p.stress >= HIGH_RISK), None)
    nodes: list[KeyNode] = []
    if first_medium is not None:
        nodes.append(KeyNode(first_medium.time, "risk_entry", "压力进入中风险区间"))
    if first_high is not None:
        nodes.append(KeyNode(first_high.time, "high_risk_entry", "压力进入高风险区间"))
    nodes.append(
        KeyNode(peak.time, "peak", f"今日预测峰值 {peak.stress:.1f}/10")
    )

    recoveries: list[KeyNode] = []
    was_high = False
    for point in points:
        was_high = was_high or point.stress >= HIGH_RISK
        if was_high and point.stress < MEDIUM_RISK:
            node = KeyNode(point.time, "recovery", "压力回落至中风险线以下")
            recoveries.append(node)
            nodes.append(node)
            was_high = False

    important_events: list[dict[str, Any]] = []
    for event in calendar_events or []:
        parsed = _event_time(event, timezone_value)
        if parsed is None:
            continue
        end_parsed = _minute_and_label(event.get("end_time"), timezone_value)
        title = str(event.get("summary") or "日程")[:80]
        event_type = str(
            event.get("event_type") or event.get("task_type") or "other"
        ).strip().lower()
        sanitized = {
            "time": parsed[1],
            "minute": parsed[0],
            "summary": title,
            "event_type": event_type,
            "start_time": event.get("start_time"),
            "end_time": event.get("end_time"),
            "end_time_local": end_parsed[1] if end_parsed else parsed[1],
            "end_minute": end_parsed[0] if end_parsed else parsed[0],
        }
        important_events.append(sanitized)
        nodes.append(KeyNode(parsed[1], "calendar_event", f"日程：{title}"))

    normalized_windows = []
    for window in warning_windows or []:
        normalized = dict(window)
        for key in ("target_time", "valid_until", "risk_time"):
            parsed = _minute_and_label(window.get(key), timezone_value)
            if parsed is not None:
                normalized[f"{key}_local"] = parsed[1]
        normalized_windows.append(normalized)
        label = normalized.get("risk_time_local") or normalized.get("target_time_local")
        if label:
            nodes.append(KeyNode(str(label), "warning_window", "主动关怀提醒窗口"))

    deduped: list[KeyNode] = []
    seen: set[tuple[str, str, str]] = set()
    for node in sorted(nodes, key=lambda item: (_minute_and_label(item.time) or (9999, ""))[0]):
        key = (node.time, node.type, node.text)
        if key not in seen:
            deduped.append(node)
            seen.add(key)

    return CurveAnalysis(
        point_count=len(points),
        peak_stress=round(peak.stress, 2),
        peak_stress_time=peak.time,
        current_stress=round(current.stress, 2),
        current_vitality=(
            round(current.vitality, 2) if current.vitality is not None else None
        ),
        current_time=current.time,
        first_medium_risk_time=first_medium.time if first_medium else None,
        first_high_risk_time=first_high.time if first_high else None,
        warning_windows=tuple(normalized_windows),
        recovery_points=tuple(recoveries),
        important_calendar_events=tuple(important_events),
        key_nodes=tuple(deduped),
    )


def time_to_minute(value: Any) -> int | None:
    parsed = _minute_and_label(value)
    return parsed[0] if parsed else None
