"""Shared defaults for the sqlite-auth-deployment style forecast chart."""

FIGSIZE = (14.4, 8.1)
S_PANEL_HEIGHT_RATIO = 3
E_PANEL_HEIGHT_RATIO = 1
PLOT_DPI = 120

EVENT_COLOR_MAP = {
    "course": ("#4169E1", "课程"),
    "task": ("#DC143C", "任务"),
    "sleep": ("#191970", "睡眠"),
    "nap": ("#20B2AA", "午休"),
    "meal": ("#3CB371", "就餐"),
    "rest": ("#BDB76B", "休息"),
    "gym": ("#FF8C00", "运动"),
    "library": ("#8A2BE2", "自习"),
}

EVENT_LABEL_Y_OFFSETS = (0.95, 0.86)
