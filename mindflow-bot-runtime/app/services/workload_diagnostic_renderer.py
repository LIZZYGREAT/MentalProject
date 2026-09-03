"""Server-side research charts for Workload, Forecast and EMA diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from io import BytesIO
import math
from pathlib import Path
import statistics
from typing import Any


_CJK_FONT_PATHS = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
)
_PREFERRED_CJK_FONT_NAMES = (
    "Noto Sans CJK SC",
    "Noto Sans CJK JP",
    "Source Han Sans SC",
    "Microsoft YaHei",
    "SimHei",
)


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

    @staticmethod
    def _resolve_font_name(font_manager) -> str:
        available = {font.name for font in font_manager.fontManager.ttflist}
        for name in _PREFERRED_CJK_FONT_NAMES:
            if name in available:
                return name
        for path in _CJK_FONT_PATHS:
            if not path.exists():
                continue
            try:
                font_manager.fontManager.addfont(str(path))
                return str(font_manager.FontProperties(fname=str(path)).get_name())
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

        return plt, WorkloadDiagnosticRenderer._resolve_font_name(font_manager)

    @staticmethod
    def _rc(font_name: str) -> dict[str, Any]:
        return {
            "font.family": "sans-serif",
            "font.sans-serif": [font_name, "DejaVu Sans"],
            "axes.unicode_minus": False,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }

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
        axis.grid(True, linestyle="--", alpha=0.25)

    @staticmethod
    def _png(figure, plt) -> bytes:
        output = BytesIO()
        try:
            figure.savefig(output, format="png", bbox_inches="tight", dpi=140)
            return output.getvalue()
        finally:
            plt.close(figure)

    def demand_vs_forecast(self, payload: dict[str, Any]) -> bytes:
        series = self._prepare(payload)
        plt, font_name = self._pyplot()
        with plt.rc_context(self._rc(font_name)):
            figure, (demand_axis, stress_axis) = plt.subplots(
                2,
                1,
                figsize=(14.4, 8.1),
                sharex=True,
                gridspec_kw={"height_ratios": [1, 1.35]},
            )
            demand_axis.fill_between(
                series.x, 0, series.workload, color="#0f766e", alpha=0.16
            )
            demand_axis.plot(
                series.x, series.workload, color="#0f766e", linewidth=2.0,
                label="Workload Demand W(t)",
            )
            demand_axis.set_ylabel("任务需求代理量（0–1）", weight="bold")
            demand_axis.set_ylim(0, 1.05)
            demand_axis.legend(loc="upper left")
            stress_axis.plot(
                series.x, series.forecast, color="royalblue", linewidth=2.2,
                label="Forecast Stress",
            )
            stress_axis.set_ylabel("预测压力（0–10）", weight="bold")
            stress_axis.set_ylim(0, 10.5)
            stress_axis.set_xlabel("日期与时间")
            stress_axis.legend(loc="upper left")
            multiple_days = series.x[0].date() != series.x[-1].date()
            self._decorate_time_axis(demand_axis, multiple_days=multiple_days)
            self._decorate_time_axis(stress_axis, multiple_days=multiple_days)
            participant = payload.get("participant_code") or payload.get("participant_id") or "全部参与者"
            figure.suptitle(
                f"Workload Demand 与 Forecast Stress（{participant}）",
                fontsize=16,
                weight="bold",
            )
            figure.tight_layout()
            return self._png(figure, plt)

    def forecast_vs_ema(self, payload: dict[str, Any]) -> bytes:
        series = self._prepare(payload)
        plt, font_name = self._pyplot()
        with plt.rc_context(self._rc(font_name)):
            figure, axis = plt.subplots(figsize=(14.4, 8.1))
            axis.plot(
                series.x, series.forecast, color="royalblue", linewidth=2.1,
                label="Forecast Stress（连续曲线）",
            )
            if series.ema_x:
                axis.scatter(
                    series.ema_x, series.ema_actual, color="#dc2626", s=48,
                    edgecolors="white", linewidths=0.8, zorder=4,
                    label=f"Actual EMA（n={len(series.ema_x)}）",
                )
            axis.set_ylim(0, 10.5)
            axis.set_ylabel("压力（0–10）", weight="bold")
            axis.set_xlabel("日期与时间")
            self._decorate_time_axis(
                axis, multiple_days=series.x[0].date() != series.x[-1].date()
            )
            axis.legend(loc="upper left")
            tolerance = payload.get("match_tolerance_minutes", 5)
            participant = payload.get("participant_code") or payload.get("participant_id") or "全部参与者"
            axis.set_title(
                f"Forecast 与 Actual EMA · {participant} · 匹配容差 ±{tolerance} 分钟",
                fontsize=15,
                weight="bold",
            )
            figure.tight_layout()
            return self._png(figure, plt)

    def residual(self, payload: dict[str, Any]) -> bytes | None:
        series = self._prepare(payload)
        if not series.ema_x:
            return None
        plt, font_name = self._pyplot()
        with plt.rc_context(self._rc(font_name)):
            figure, axis = plt.subplots(figsize=(14.4, 5.2))
            colors = ["#dc2626" if value > 0 else "#0f766e" for value in series.residual]
            axis.axhline(0, color="#334155", linewidth=1.2)
            axis.vlines(series.ema_x, 0, series.residual, colors=colors, alpha=0.6)
            axis.scatter(series.ema_x, series.residual, color=colors, s=42, zorder=3)
            axis.set_ylabel("Residual（Actual − Forecast）", weight="bold")
            axis.set_xlabel("日期与时间")
            self._decorate_time_axis(
                axis, multiple_days=series.x[0].date() != series.x[-1].date()
            )
            axis.set_title("Forecast Residual 诊断", fontsize=15, weight="bold")
            figure.tight_layout()
            return self._png(figure, plt)

    def render(self, payload: dict[str, Any]) -> WorkloadDiagnosticImages:
        return WorkloadDiagnosticImages(
            demand_vs_forecast_png=self.demand_vs_forecast(payload),
            forecast_vs_ema_png=self.forecast_vs_ema(payload),
            residual_png=self.residual(payload),
        )
