"""Server-side research charts for Workload, Forecast and EMA diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from io import BytesIO
import logging
import math
from pathlib import Path
import statistics
from typing import Any


logger = logging.getLogger(__name__)

_CJK_FONT_PATHS = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
)
_PREFERRED_CJK_FONT_NAMES = (
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "Source Han Sans SC",
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "SimHei",
)
_REQUIRED_CHART_GLYPHS = (
    "任务负荷预测压力观测样本不足"
    "日期时间当前实际残差参与者连续"
)

_INK = "#29332f"
_MUTED = "#6b756f"
_GRID = "#dfe5e1"
_WORKLOAD = "#397668"
_FORECAST = "#4169e1"
_ACTUAL = "#c94343"


class WorkloadDataNotFoundError(LookupError):
    """The requested research window has no plottable workload series."""


@dataclass(frozen=True)
class WorkloadDiagnosticImages:
    demand_vs_forecast_png: bytes
    forecast_vs_ema_png: bytes
    residual_png: bytes | None


@dataclass(frozen=True)
class _Series:
    x: tuple[datetime, ...]
    workload: tuple[float, ...]
    forecast: tuple[float, ...]
    ema_x: tuple[datetime, ...]
    ema_actual: tuple[float, ...]
    ema_forecast: tuple[float, ...]
    residual: tuple[float, ...]


class WorkloadDiagnosticRenderer:
    """Keep unlike units in separate panels and EMA as discrete observations."""

    def __init__(self) -> None:
        # AdminAPI creates this renderer during application startup, so font
        # discovery and the warmup canvas finish before any chart request.
        self._pyplot()

    @staticmethod
    def _font_supports(path: str | Path, required: str) -> bool:
        from matplotlib.ft2font import FT2Font

        try:
            glyphs = FT2Font(str(path)).get_charmap()
        except (OSError, RuntimeError, ValueError):
            return False
        return all(ord(character) in glyphs for character in required)

    @staticmethod
    def _resolve_font(font_manager) -> tuple[str, str]:
        for path in _CJK_FONT_PATHS:
            if not path.exists() or not WorkloadDiagnosticRenderer._font_supports(
                path, _REQUIRED_CHART_GLYPHS
            ):
                continue
            try:
                font_manager.fontManager.addfont(str(path))
                name = str(font_manager.FontProperties(fname=str(path)).get_name())
                return name, str(path.resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        for name in _PREFERRED_CJK_FONT_NAMES:
            try:
                resolved = font_manager.findfont(
                    font_manager.FontProperties(family=name),
                    fallback_to_default=False,
                )
            except (OSError, RuntimeError, ValueError):
                continue
            if not WorkloadDiagnosticRenderer._font_supports(
                resolved, _REQUIRED_CHART_GLYPHS
            ):
                continue
            try:
                font_manager.fontManager.addfont(str(resolved))
                resolved_name = str(
                    font_manager.FontProperties(fname=str(resolved)).get_name()
                )
            except (OSError, RuntimeError, ValueError):
                continue
            return resolved_name, str(Path(resolved).resolve())
        raise RuntimeError("workload_diagnostic_cjk_font_unavailable")

    @staticmethod
    @lru_cache(maxsize=1)
    def _pyplot():
        import matplotlib

        matplotlib.use("Agg")
        from matplotlib import font_manager
        from matplotlib import pyplot as plt

        font_name, font_path = WorkloadDiagnosticRenderer._resolve_font(font_manager)
        # Force the selected CJK font through a real first canvas draw.  This
        # warms Matplotlib's font lookup before the first Admin HTTP request,
        # avoiding the previous first-render-only tofu/missing-glyph result.
        with plt.rc_context(WorkloadDiagnosticRenderer._rc(font_name)):
            warmup = plt.figure(figsize=(0.2, 0.2), dpi=40)
            warmup.text(
                0,
                0,
                _REQUIRED_CHART_GLYPHS + "～±·-",
                fontfamily=font_name,
            )
            warmup.canvas.draw()
            plt.close(warmup)
        logger.info(
            "workload_diagnostic_font_selected",
            extra={"font_name": font_name, "font_path": font_path},
        )
        return plt, font_name, font_path

    @staticmethod
    def _rc(font_name: str) -> dict[str, Any]:
        return {
            "font.family": "sans-serif",
            "font.sans-serif": [font_name, "DejaVu Sans"],
            "axes.unicode_minus": False,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": _GRID,
            "axes.labelcolor": _INK,
            "axes.titlecolor": _INK,
            "text.color": _INK,
            "xtick.color": _MUTED,
            "ytick.color": _MUTED,
            "axes.titlesize": 15,
            "axes.labelsize": 12,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "legend.fontsize": 10,
        }

    @staticmethod
    def _apply_admin_research_style(*axes: Any) -> None:
        """Apply the restrained visual language used by the Forecast chart."""

        for axis in axes:
            axis.spines["top"].set_visible(False)
            axis.spines["right"].set_visible(False)
            axis.spines["left"].set_color(_GRID)
            axis.spines["bottom"].set_color(_GRID)
            axis.tick_params(axis="both", length=0, pad=7)
            axis.grid(axis="y", color=_GRID, linewidth=0.8, alpha=0.62)
            axis.set_axisbelow(True)

    @staticmethod
    def _style_legend(axis: Any, *, location: str = "upper right") -> None:
        legend = axis.legend(loc=location, frameon=True)
        if legend is not None:
            legend.get_frame().set_facecolor("white")
            legend.get_frame().set_edgecolor(_GRID)
            legend.get_frame().set_linewidth(0.8)
            legend.get_frame().set_alpha(0.94)

    @staticmethod
    def _timestamp(local_date: Any, time_value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(
                f"{str(local_date)[:10]}T{str(time_value)[:5]}:00"
            )
        except (TypeError, ValueError):
            return None

    def _prepare(self, payload: dict[str, Any]) -> _Series:
        forecast_rows: list[tuple[datetime, float, float]] = []
        for row in payload.get("series") or []:
            timestamp = self._timestamp(row.get("local_date"), row.get("time"))
            try:
                workload = float(row.get("workload"))
                forecast = float(row.get("forecast_stress"))
            except (TypeError, ValueError):
                continue
            if (
                timestamp is not None
                and math.isfinite(workload)
                and math.isfinite(forecast)
                and 0 <= workload <= 1
                and 0 <= forecast <= 10
            ):
                forecast_rows.append((timestamp, workload, forecast))
        if not forecast_rows:
            raise WorkloadDataNotFoundError("workload_data_not_found")
        # A cohort query may contain several participants at the same time.
        # Aggregate those points instead of drawing a false connecting line
        # from one participant to the next.
        forecast_buckets: dict[datetime, list[tuple[float, float]]] = {}
        for timestamp, workload, forecast in forecast_rows:
            forecast_buckets.setdefault(timestamp, []).append((workload, forecast))
        forecast_rows = [
            (
                timestamp,
                statistics.fmean(value[0] for value in values),
                statistics.fmean(value[1] for value in values),
            )
            for timestamp, values in sorted(forecast_buckets.items())
        ]

        ema_rows: list[tuple[datetime, float, float, float]] = []
        for row in payload.get("actual_ema") or []:
            timestamp = self._timestamp(row.get("local_date"), row.get("time"))
            try:
                actual = float(row.get("actual_stress"))
                predicted = float(row.get("forecast_stress"))
                residual = float(row.get("residual"))
            except (TypeError, ValueError):
                continue
            if timestamp is not None and all(
                math.isfinite(value) for value in (actual, predicted, residual)
            ):
                ema_rows.append((timestamp, actual, predicted, residual))
        ema_buckets: dict[datetime, list[tuple[float, float, float]]] = {}
        for timestamp, actual, predicted, residual in ema_rows:
            ema_buckets.setdefault(timestamp, []).append((actual, predicted, residual))
        ema_rows = [
            (
                timestamp,
                statistics.fmean(value[0] for value in values),
                statistics.fmean(value[1] for value in values),
                statistics.fmean(value[2] for value in values),
            )
            for timestamp, values in sorted(ema_buckets.items())
        ]
        return _Series(
            x=tuple(row[0] for row in forecast_rows),
            workload=tuple(row[1] for row in forecast_rows),
            forecast=tuple(row[2] for row in forecast_rows),
            ema_x=tuple(row[0] for row in ema_rows),
            ema_actual=tuple(row[1] for row in ema_rows),
            ema_forecast=tuple(row[2] for row in ema_rows),
            residual=tuple(row[3] for row in ema_rows),
        )

    @staticmethod
    def _decorate_time_axis(axis, *, multiple_days: bool) -> None:
        from matplotlib import dates as mdates

        axis.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=12))
        axis.xaxis.set_major_formatter(
            mdates.DateFormatter("%m-%d\n%H:%M" if multiple_days else "%H:%M")
        )

    @staticmethod
    def _png(figure, plt) -> bytes:
        output = BytesIO()
        try:
            # Fixed canvas bounds keep all workload cards pixel-aligned; each
            # renderer sets explicit subplot margins before reaching here.
            figure.savefig(output, format="png", dpi=140)
            return output.getvalue()
        finally:
            plt.close(figure)

    def demand_vs_forecast(self, payload: dict[str, Any]) -> bytes:
        series = self._prepare(payload)
        plt, font_name, _font_path = self._pyplot()
        with plt.rc_context(self._rc(font_name)):
            figure, (demand_axis, stress_axis) = plt.subplots(
                2,
                1,
                figsize=(15, 8),
                sharex=True,
                gridspec_kw={"height_ratios": [1, 1.18], "hspace": 0.16},
            )
            demand_axis.fill_between(
                series.x, 0, series.workload, color=_WORKLOAD, alpha=0.08
            )
            demand_axis.plot(
                series.x,
                series.workload,
                color=_WORKLOAD,
                linewidth=2.25,
                label="任务负荷 W(t)",
            )
            demand_axis.set_ylabel("任务负荷 W(t)\n0-1", weight="bold")
            demand_axis.set_ylim(0, 1.05)
            self._style_legend(demand_axis)
            stress_axis.plot(
                series.x,
                series.forecast,
                color=_FORECAST,
                linewidth=2.45,
                label="Forecast",
            )
            stress_axis.set_ylabel("预测压力\n0-10", weight="bold")
            stress_axis.set_ylim(0, 10.5)
            stress_axis.set_xlabel("日期与时间")
            self._style_legend(stress_axis)
            multiple_days = series.x[0].date() != series.x[-1].date()
            self._decorate_time_axis(demand_axis, multiple_days=multiple_days)
            self._decorate_time_axis(stress_axis, multiple_days=multiple_days)
            self._apply_admin_research_style(demand_axis, stress_axis)
            figure.subplots_adjust(left=0.10, right=0.975, top=0.97, bottom=0.11)
            return self._png(figure, plt)

    def forecast_vs_ema(self, payload: dict[str, Any]) -> bytes:
        series = self._prepare(payload)
        plt, font_name, _font_path = self._pyplot()
        with plt.rc_context(self._rc(font_name)):
            figure, axis = plt.subplots(figsize=(15, 7.2))
            axis.plot(
                series.x,
                series.forecast,
                color=_FORECAST,
                linewidth=2.45,
                label="Forecast 连续预测",
            )
            if series.ema_x:
                axis.scatter(
                    series.ema_x,
                    series.ema_actual,
                    color=_ACTUAL,
                    s=56,
                    edgecolors="#8f2f2f",
                    linewidths=0.8,
                    zorder=4,
                    label=f"Actual EMA 观测 (n={len(series.ema_x)})",
                )
            axis.set_ylim(0, 10.5)
            axis.set_ylabel("压力 0-10", weight="bold")
            axis.set_xlabel("日期与时间")
            self._decorate_time_axis(
                axis, multiple_days=series.x[0].date() != series.x[-1].date()
            )
            self._apply_admin_research_style(axis)
            self._style_legend(axis)
            figure.subplots_adjust(left=0.09, right=0.975, top=0.97, bottom=0.15)
            return self._png(figure, plt)

    def residual(self, payload: dict[str, Any]) -> bytes | None:
        series = self._prepare(payload)
        if len(series.ema_x) < 2:
            return None
        plt, font_name, _font_path = self._pyplot()
        with plt.rc_context(self._rc(font_name)):
            figure, axis = plt.subplots(figsize=(14.4, 5.2))
            colors = [_ACTUAL if value > 0 else _WORKLOAD for value in series.residual]
            axis.axhline(0, color=_INK, linewidth=1.1)
            axis.vlines(series.ema_x, 0, series.residual, colors=colors, alpha=0.6)
            axis.scatter(series.ema_x, series.residual, color=colors, s=42, zorder=3)
            axis.set_ylabel("Residual（Actual − Forecast）", weight="bold")
            axis.set_xlabel("日期与时间")
            self._decorate_time_axis(
                axis, multiple_days=series.x[0].date() != series.x[-1].date()
            )
            self._apply_admin_research_style(axis)
            figure.subplots_adjust(left=0.10, right=0.975, top=0.96, bottom=0.20)
            return self._png(figure, plt)

    def render(self, payload: dict[str, Any]) -> WorkloadDiagnosticImages:
        return WorkloadDiagnosticImages(
            demand_vs_forecast_png=self.demand_vs_forecast(payload),
            forecast_vs_ema_png=self.forecast_vs_ema(payload),
            residual_png=self.residual(payload),
        )
