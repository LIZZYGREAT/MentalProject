"""Model-aware Matplotlib renderer for persisted MindFlow forecasts."""

from __future__ import annotations

from io import BytesIO
import math
from typing import Any

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


class PressureCurveRenderer:
    """Render only states and inputs that the active forecast model computes."""

    @staticmethod
    def _pyplot():
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import font_manager
        from matplotlib import pyplot as plt

        available = {font.name for font in font_manager.fontManager.ttflist}
        preferred = (
            "Noto Sans CJK SC",
            "Source Han Sans SC",
            "Microsoft YaHei",
            "SimHei",
            "DejaVu Sans",
        )
        font_name = next(
            (name for name in preferred if name in available), "DejaVu Sans"
        )
        return plt, font_name

    def _draw_core_plot(
        self,
        curve: list[dict[str, Any]],
        analysis: CurveAnalysis,
        model_output: dict[str, Any] | None = None,
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

        with plt.rc_context(
            {
                "font.family": font_name,
                "font.sans-serif": [font_name],
                "axes.unicode_minus": False,
                "figure.facecolor": "white",
            }
        ):
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
                label="真实 M0 压力 S(t)" if context.variant == "m0" else "压力值 S(t)",
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
                    label="瞬时平衡 S_eq(t)",
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
                    label="压力 90% 预测区间",
                )

            equilibrium = context.stress_baseline * 10.0
            threshold = context.stress_threshold * 10.0
            stress_axis.axhline(
                equilibrium,
                color="gray",
                linestyle=":",
                linewidth=1.5,
                label=f"用户平衡值 S*={equilibrium:g}",
            )
            stress_axis.axhline(
                threshold,
                color="red",
                linestyle="--",
                linewidth=1.5,
                label=f"关怀观察线={threshold:g}",
            )

            lower_limit = max(0.0, min(stress_values) - 10.0)
            y_range = max(MIN_PLOT_Y_RANGE, max(stress_values) - lower_limit)
            upper_limit = max(max(stress_values) + y_range * 0.45, threshold + 10.0)
            y_range = upper_limit - lower_limit
            stress_axis.set_ylim(lower_limit, upper_limit)
            stress_axis.set_ylabel(
                "心理压力 (S, 0-100)", color="royalblue", fontsize=13, weight="bold"
            )
            stress_axis.tick_params(axis="y", labelcolor="royalblue")
            stress_axis.grid(True, linestyle="--", alpha=0.3)

            transform = stress_axis.get_xaxis_transform()
            for index, event in enumerate(analysis.important_calendar_events):
                start = int(event["minute"])
                end = int(event.get("end_minute", start))
                if end < start:
                    end = 1440
                event_type = str(event.get("event_type") or "other")
                color, type_name = EVENT_COLOR_MAP.get(
                    event_type, ("#7f7f7f", "其他")
                )
                alpha = 0.30 if event_type == "sleep" else 0.20
                start_hour = start / 60.0
                end_hour = end / 60.0
                stress_axis.axvspan(start_hour, end_hour, color=color, alpha=alpha)
                secondary_axis.axvspan(start_hour, end_hour, color=color, alpha=alpha)
                midpoint = start_hour + (end_hour - start_hour) / 2.0
                y_position = EVENT_LABEL_Y_OFFSETS[index % len(EVENT_LABEL_Y_OFFSETS)]
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
                    label="M3 恢复债状态 F(t)",
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

            current_minute = time_to_minute(analysis.current_time)
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
                secondary_axis.axvline(
                    current_hour,
                    color="#475569",
                    linestyle=":",
                    linewidth=1.2,
                    alpha=0.75,
                )

            stress_axis.legend(
                loc="center left", bbox_to_anchor=(0.01, 0.73), fontsize=9
            )

            if context.has_dynamic_vitality:
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
                    label="主观活力 V(t)",
                )
                critical = context.energy_critical * 10.0
                secondary_axis.axhline(
                    critical,
                    color="crimson",
                    linestyle="-.",
                    linewidth=1.5,
                    label=f"活力参考线={critical:g}",
                )
                secondary_axis.fill_between(
                    times, 0, critical, color="red", alpha=0.10
                )
                secondary_axis.set_ylabel(
                    "主观活力 (V)", color="mediumseagreen", fontsize=13, weight="bold"
                )
                secondary_axis.set_ylim(0, 105)
                secondary_axis.legend(loc="lower left", fontsize=10)
                title = f"{context.variant.upper()} 压力与活力演化模型（真实预测）"
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
                    label="事件压力 U(t)",
                )
                secondary_axis.plot(
                    times,
                    anticipation,
                    color="#d97706",
                    linewidth=1.5,
                    label="事前预期 A(t)",
                )
                secondary_axis.plot(
                    times,
                    aftermath,
                    color="#7c3aed",
                    linewidth=1.5,
                    label="事后残留 H(t)",
                )
                secondary_axis.set_ylabel(
                    "M0 公式输入 (0-1)", fontsize=12, weight="bold"
                )
                secondary_axis.set_ylim(0, 1.05)
                secondary_axis.legend(loc="upper left", fontsize=9, ncol=3)
                title = "M0 压力时变平衡模型（真实预测）"

            secondary_axis.grid(True, linestyle="--", alpha=0.3)
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
                    alpha=0.65,
                    label="预警风险置信度",
                )
                confidence_axis.set_ylim(0, 1.05)
                confidence_axis.set_ylabel(
                    "预警置信度 (0-1)", color="orange", fontsize=11
                )
                confidence_axis.tick_params(axis="y", labelcolor="orange")
                confidence_axis.legend(
                    loc="center left", bbox_to_anchor=(0.01, 0.59), fontsize=9
                )

            figure.suptitle(title, fontsize=16, weight="bold")
            figure.tight_layout()
            return figure

    def render(
        self,
        curve: list[dict[str, Any]],
        analysis: CurveAnalysis,
        model_output: dict[str, Any] | None = None,
    ) -> bytes:
        plt, _font_name = self._pyplot()
        figure = self._draw_core_plot(curve, analysis, model_output)
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
