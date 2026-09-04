"""Model-aware Matplotlib renderer for persisted MindFlow forecasts."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
import logging
import math
from pathlib import Path
from typing import Any

from app.services.admin_chart_theme import (
    admin_chart_rc,
    apply_admin_chart_axes,
    style_admin_chart_legend,
)
from app.services.curve_analysis import (
    CurveAnalysis,
    curve_points,
    forecast_model_context,
    time_to_minute,
)
from settings.model_defaults import MIN_PLOT_Y_RANGE
from settings.visual_defaults import (
    EVENT_COLOR_MAP,
    EVENT_LABEL_Y_OFFSETS,
    E_PANEL_HEIGHT_RATIO,
    FIGSIZE,
    PLOT_DPI,
    S_PANEL_HEIGHT_RATIO,
)


logger = logging.getLogger(__name__)

_CJK_FONT_PATHS = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
)

_PREFERRED_CJK_FONT_NAMES = (
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "Noto Sans CJK TC",
    "Source Han Sans SC",
    "Microsoft YaHei",
    "SimHei",
)

_EVENT_TYPE_ALIASES = {
    "class": "course",
    "meeting": "task",
    "work": "task",
    "exercise": "gym",
    "workout": "gym",
    "study": "library",
    "self_study": "library",
    "课程": "course",
    "任务": "task",
    "睡眠": "sleep",
    "午休": "nap",
    "就餐": "meal",
    "休息": "rest",
    "运动": "gym",
    "自习": "library",
}


class PressureCurveRenderer:
    """Render only states and inputs that the active forecast model computes."""

    @staticmethod
    def _resolve_font_name(font_manager) -> str:
        available = {
            font.name for font in font_manager.fontManager.ttflist
        }
        for name in _PREFERRED_CJK_FONT_NAMES:
            if name in available:
                return name

        for path in _CJK_FONT_PATHS:
            if not path.exists():
                continue
            try:
                font_manager.fontManager.addfont(str(path))
                name = font_manager.FontProperties(fname=str(path)).get_name()
                if name:
                    return str(name)
            except (OSError, RuntimeError, ValueError):
                continue
        return "DejaVu Sans"

    @staticmethod
    @lru_cache(maxsize=1)
    def _pyplot():
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import font_manager
        from matplotlib import pyplot as plt

        font_name = PressureCurveRenderer._resolve_font_name(font_manager)
        if font_name == "DejaVu Sans":
            logger.warning(
                "pressure_curve_cjk_font_unavailable",
                extra={"fallback_font": font_name},
            )
        else:
            logger.info(
                "pressure_curve_font_selected",
                extra={"font_name": font_name},
            )
        return plt, font_name

    @staticmethod
    def _font_rc(font_name: str) -> dict[str, Any]:
        return admin_chart_rc(font_name)

    @staticmethod
    def _apply_reference_style(*axes: Any) -> None:
        """Apply the restrained white-background grid used by the reference."""

        apply_admin_chart_axes(*axes)

    @staticmethod
    def _event_style(event: dict[str, Any]) -> tuple[str, str, float]:
        event_type = str(event.get("event_type") or "other").strip().lower()
        event_type = _EVENT_TYPE_ALIASES.get(event_type, event_type)
        if event_type in EVENT_COLOR_MAP:
            color, type_name = EVENT_COLOR_MAP[event_type]
        else:
            color = (
                "#b45309"
                if str(event.get("importance") or "").lower()
                in {"high", "critical"}
                else "#64748b"
            )
            type_name = "其他"
        return color, type_name, 0.30 if event_type == "sleep" else 0.18

    @staticmethod
    def _finalize_figure(figure: Any, title: str) -> None:
        figure.suptitle(title, fontsize=16, weight="bold")
        figure.tight_layout(rect=(0, 0, 1, 0.97))

    def _draw_core_plot(
        self,
        curve: list[dict[str, Any]],
        analysis: CurveAnalysis,
        model_output: dict[str, Any] | None = None,
        *,
        stress_only: bool = False,
    ):
        """Build a plot whose panels follow the active M0--M3 state definition."""

        plt, font_name = self._pyplot()
        context = forecast_model_context(model_output)
        points = curve_points(curve)
        times = [point.minute / 60.0 for point in points]
        stress_values = [point.stress * 10.0 for point in points]
        confidence_values = [
            point.confidence if point.confidence is not None else math.nan
            for point in points
        ]

        with plt.rc_context(self._font_rc(font_name)):
            if stress_only:
                figure, stress_axis = plt.subplots(
                    1, 1, figsize=(FIGSIZE[0], 6), dpi=PLOT_DPI
                )
                secondary_axis = None
            else:
                figure, (stress_axis, secondary_axis) = plt.subplots(
                    2,
                    1,
                    figsize=FIGSIZE,
                    dpi=PLOT_DPI,
                    sharex=True,
                    gridspec_kw={
                        "height_ratios": [S_PANEL_HEIGHT_RATIO, E_PANEL_HEIGHT_RATIO]
                    },
                )

            stress_axis.plot(
                times,
                stress_values,
                color="royalblue",
                linewidth=2.5,
                label="压力值 S(t)",
            )

            equilibrium_values = [
                point.stress_equilibrium * 10.0
                if point.stress_equilibrium is not None
                else math.nan
                for point in points
            ]
            if any(math.isfinite(value) for value in equilibrium_values):
                stress_axis.plot(
                    times,
                    equilibrium_values,
                    color="#0f766e",
                    linewidth=1.35,
                    linestyle="--",
                    alpha=0.85,
                    label="动态压力基准",
                )

            interval_lower = [
                point.stress_interval_lower * 10.0
                if point.stress_interval_lower is not None
                else math.nan
                for point in points
            ]
            interval_upper = [
                point.stress_interval_upper * 10.0
                if point.stress_interval_upper is not None
                else math.nan
                for point in points
            ]
            if any(
                math.isfinite(low) and math.isfinite(high)
                for low, high in zip(interval_lower, interval_upper)
            ):
                stress_axis.fill_between(
                    times,
                    interval_lower,
                    interval_upper,
                    color="royalblue",
                    alpha=0.10,
                    label="压力预测区间（90%）",
                )

            equilibrium = context.stress_baseline * 10.0
            threshold = context.stress_threshold * 10.0
            stress_axis.axhline(
                equilibrium,
                color="gray",
                linestyle=":",
                linewidth=1.5,
                label=f"压力基准值={equilibrium:g}",
            )
            stress_axis.axhline(
                threshold,
                color="red",
                linestyle="--",
                linewidth=1.5,
                label=f"压力关注线={threshold:g}",
            )

            lower_limit = max(0.0, min(stress_values) - 10.0)
            y_range = max(MIN_PLOT_Y_RANGE, max(stress_values) - lower_limit)
            upper_limit = max(max(stress_values) + y_range * 0.45, threshold + 10.0)
            y_range = upper_limit - lower_limit
            stress_axis.set_ylim(lower_limit, upper_limit)
            stress_axis.set_ylabel(
                "心理压力（0–100）", color="royalblue", fontsize=13, weight="bold"
            )
            stress_axis.tick_params(axis="y", labelcolor="royalblue")
            self._apply_reference_style(stress_axis)

            transform = stress_axis.get_xaxis_transform()
            for index, event in enumerate(analysis.important_calendar_events):
                start = int(event["minute"])
                end = int(event.get("end_minute", start))
                if end < start:
                    end = 1440
                color, type_name, alpha = self._event_style(event)
                start_hour = start / 60.0
                end_hour = end / 60.0
                stress_axis.axvspan(start_hour, end_hour, color=color, alpha=alpha)
                if secondary_axis is not None:
                    secondary_axis.axvspan(
                        start_hour, end_hour, color=color, alpha=alpha
                    )
                midpoint = start_hour + (end_hour - start_hour) / 2.0
                label_lanes = (*EVENT_LABEL_Y_OFFSETS, 0.77)
                y_position = label_lanes[index % len(label_lanes)]
                label = (
                    f"[{type_name}] {str(event['summary'])[:18]}\n"
                    f"{event['time']}-{event.get('end_time_local', event['time'])}"
                )
                stress_axis.text(
                    midpoint,
                    y_position,
                    label,
                    transform=transform,
                    ha="center",
                    va="top",
                    fontsize=9,
                    color=color,
                    weight="bold",
                    bbox={
                        "facecolor": "white",
                        "alpha": 0.9,
                        "edgecolor": color,
                        "boxstyle": "round,pad=0.3",
                    },
                )

            # F is a state only in M3. Never paint a legacy fatigue penalty for M0.
            penalty_values = [point.penalty for point in points]
            if context.has_fatigue_state and any(value > 0 for value in penalty_values):
                maximum_penalty = max(penalty_values)
                scale = (y_range * 0.20) / maximum_penalty
                scaled_penalty = [
                    value * scale + lower_limit for value in penalty_values
                ]
                stress_axis.fill_between(
                    times,
                    lower_limit,
                    scaled_penalty,
                    color="crimson",
                    alpha=0.25,
                    label="持续负荷影响",
                    step="post",
                )

            for window in analysis.warning_windows:
                payload = (
                    window.get("payload")
                    if isinstance(window.get("payload"), dict)
                    else {}
                )
                warning_minute = time_to_minute(
                    window.get("risk_time_local") or payload.get("time")
                )
                if warning_minute is None:
                    continue
                try:
                    warning_stress = float(payload.get("S"))
                except (TypeError, ValueError):
                    nearest = min(
                        points, key=lambda point: abs(point.minute - warning_minute)
                    )
                    warning_stress = nearest.stress * 10.0
                warning_type = str(
                    payload.get("type") or f"{window.get('warning_level', '')}级预警"
                )
                warning_hour = warning_minute / 60.0
                stress_axis.plot(
                    warning_hour,
                    warning_stress,
                    marker="o",
                    color="red",
                    markersize=7,
                    zorder=5,
                )
                stress_axis.annotate(
                    warning_type,
                    xy=(warning_hour, warning_stress),
                    xytext=(warning_hour, warning_stress + y_range * 0.05),
                    color="red",
                    fontsize=10,
                    weight="bold",
                    zorder=6,
                    arrowprops={"arrowstyle": "->", "color": "red"},
                )

            show_current_time = bool(
                (model_output or {}).get("_render_is_today", True)
            )
            current_minute = (
                time_to_minute(analysis.current_time) if show_current_time else None
            )
            if current_minute is not None:
                current_hour = current_minute / 60.0
                stress_axis.axvline(
                    current_hour,
                    color="#475569",
                    linestyle=":",
                    linewidth=1.2,
                    alpha=0.75,
                    label="当前时刻",
                )
                if secondary_axis is not None:
                    secondary_axis.axvline(
                        current_hour,
                        color="#475569",
                        linestyle=":",
                        linewidth=1.2,
                        alpha=0.75,
                    )

            style_admin_chart_legend(
                stress_axis,
                location="center left",
                bbox_to_anchor=(0.01, 0.75),
            )

            if stress_only:
                title = "压力趋势"
                stress_axis.set_xlabel("时间 (24h)", fontsize=12)
                stress_axis.set_xlim(min(times), max(times))
                stress_axis.set_xticks(range(0, 25, 2))
                stress_axis.set_xticklabels(
                    [f"{hour:02d}:00" for hour in range(0, 25, 2)], rotation=45
                )
            elif context.has_dynamic_vitality:
                vitality_values = [
                    point.vitality * 10.0
                    if point.vitality is not None
                    else math.nan
                    for point in points
                ]
                secondary_axis.plot(
                    times,
                    vitality_values,
                    color="mediumseagreen",
                    linewidth=2.5,
                    label="活力值 V(t)",
                )
                critical = context.energy_critical * 10.0
                secondary_axis.axhline(
                    critical,
                    color="crimson",
                    linestyle="-.",
                    linewidth=1.5,
                    label=f"低活力关注线={critical:g}",
                )
                secondary_axis.fill_between(
                    times, 0, critical, color="red", alpha=0.10
                )
                secondary_axis.set_ylabel(
                    "活力值（0–100）",
                    color="mediumseagreen",
                    fontsize=13,
                    weight="bold",
                )
                secondary_axis.set_ylim(0, 105)
                style_admin_chart_legend(secondary_axis, location="lower left")
                title = "今日压力与活力趋势"
            else:
                event_input = [point.event_stress_input for point in points]
                anticipation = [point.anticipatory_input for point in points]
                aftermath = [point.post_event_input for point in points]
                secondary_axis.fill_between(
                    times,
                    0,
                    event_input,
                    color="#ef4444",
                    alpha=0.12,
                )
                secondary_axis.plot(
                    times,
                    event_input,
                    color="#dc2626",
                    linewidth=1.8,
                    label="事件影响",
                )
                secondary_axis.plot(
                    times,
                    anticipation,
                    color="#d97706",
                    linewidth=1.5,
                    label="事前影响",
                )
                secondary_axis.plot(
                    times,
                    aftermath,
                    color="#7c3aed",
                    linewidth=1.5,
                    label="事后影响",
                )
                secondary_axis.set_ylabel(
                    "压力影响强度（0–1）", fontsize=12, weight="bold"
                )
                secondary_axis.set_ylim(0, 1.05)
                style_admin_chart_legend(
                    secondary_axis, location="upper left", ncol=3
                )
                title = "今日压力趋势与影响因素"

            if secondary_axis is not None:
                self._apply_reference_style(secondary_axis)
                secondary_axis.set_xlabel("时间 (24h)", fontsize=12)
                secondary_axis.set_xlim(min(times), max(times))
                secondary_axis.set_xticks(range(0, 25, 2))
                secondary_axis.set_xticklabels(
                    [f"{hour:02d}:00" for hour in range(0, 25, 2)], rotation=45
                )

            if any(math.isfinite(value) for value in confidence_values):
                confidence_axis = stress_axis.twinx()
                confidence_axis.fill_between(
                    times, confidence_values, color="orange", alpha=0.10
                )
                confidence_axis.plot(
                    times,
                    confidence_values,
                    color="orange",
                    linestyle="--",
                    linewidth=1.5,
                    alpha=0.60,
                    label="预测置信度",
                )
                confidence_axis.set_ylim(0, 1.05)
                confidence_axis.set_ylabel(
                    "预测置信度（0–1）", color="orange", fontsize=11
                )
                confidence_axis.tick_params(axis="y", labelcolor="orange")
                style_admin_chart_legend(
                    confidence_axis,
                    location="center left",
                    bbox_to_anchor=(0.01, 0.65),
                )

            self._finalize_figure(figure, title)
            return figure

    def render(
        self,
        curve: list[dict[str, Any]],
        analysis: CurveAnalysis,
        model_output: dict[str, Any] | None = None,
        *,
        stress_only: bool = False,
    ) -> bytes:
        plt, font_name = self._pyplot()
        # Matplotlib resolves generic sans-serif families at draw/save time.
        # Keep savefig inside the same context used to create the artists or it
        # can silently fall back to DejaVu even after selecting a CJK font.
        with plt.rc_context(self._font_rc(font_name)):
            figure = self._draw_core_plot(
                curve, analysis, model_output, stress_only=stress_only
            )
            try:
                output = BytesIO()
                figure.savefig(
                    output,
                    format="png",
                    bbox_inches="tight",
                    dpi=PLOT_DPI,
                )
                return output.getvalue()
            finally:
                plt.close(figure)
