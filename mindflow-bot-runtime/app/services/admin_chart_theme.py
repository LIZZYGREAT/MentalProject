"""Shared Matplotlib theme for Admin forecast and research charts."""

from __future__ import annotations

from typing import Any


INK = "#29332f"
MUTED = "#6b756f"
GRID = "#dfe5e1"
SURFACE = "#fbfcfa"
WORKLOAD = "#397668"
FORECAST = "#4169e1"
ACTUAL = "#c94343"


def admin_chart_rc(font_name: str) -> dict[str, Any]:
    """Return the product chart typography and surface defaults."""

    return {
        "font.family": "sans-serif",
        "font.sans-serif": [font_name, "DejaVu Sans"],
        "axes.unicode_minus": False,
        "figure.facecolor": "white",
        "axes.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK,
        "axes.titlecolor": INK,
        "text.color": INK,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "axes.titlesize": 13,
        "axes.titleweight": "semibold",
        "axes.labelsize": 11,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "legend.fontsize": 10,
        "lines.solid_capstyle": "round",
        "lines.solid_joinstyle": "round",
    }


def apply_admin_chart_axes(*axes: Any) -> None:
    """Apply the restrained axes, grid and spacing used across Admin charts."""

    for axis in axes:
        if axis is None:
            continue
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.spines["left"].set_color(GRID)
        axis.spines["bottom"].set_color(GRID)
        axis.tick_params(axis="both", length=0, pad=7)
        axis.grid(True, linestyle="--", color=GRID, linewidth=0.8, alpha=0.72)
        axis.set_axisbelow(True)


def style_admin_chart_legend(
    axis: Any,
    *,
    location: str,
    bbox_to_anchor: tuple[float, float] | None = None,
    ncol: int = 1,
) -> Any:
    """Render a compact product-style legend on a chart axis."""

    kwargs: dict[str, Any] = {
        "loc": location,
        "frameon": True,
        "ncol": ncol,
        "borderpad": 0.7,
        "labelspacing": 0.6,
        "handlelength": 2.1,
    }
    if bbox_to_anchor is not None:
        kwargs["bbox_to_anchor"] = bbox_to_anchor
    legend = axis.legend(**kwargs)
    if legend is not None:
        legend.get_frame().set_facecolor("white")
        legend.get_frame().set_edgecolor(GRID)
        legend.get_frame().set_linewidth(0.8)
        legend.get_frame().set_alpha(0.94)
    return legend
