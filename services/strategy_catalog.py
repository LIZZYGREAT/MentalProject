"""Legacy-only deterministic function diagnostics for the retired S/E engine."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Dict

from strategy.course_strategy import make_C_strategy, make_f_strategy
from strategy.night_strategy import make_night_strategy
from strategy.rest_strategy import make_rest_strategy


STRATEGY_CATALOG: Dict[str, Dict[str, Any]] = {
    "f_strategy": {
        "label": "压力响应形态",
        "short_label": "压力响应",
        "description": "决定任务负荷进入模型后，压力增量如何随当前压力与精力变化。",
        "choices": [
            {
                "value": "sensitive",
                "label": "敏感型",
                "summary": "较早感知负荷，压力响应会更快抬升。",
                "effect": "低到中等压力区间也会保留较明显的响应。",
            },
            {
                "value": "dull",
                "label": "迟钝型",
                "summary": "前段变化较平，超过阈值后才逐渐增强。",
                "effect": "适合描述“起初没感觉，积累后才明显”的走势。",
            },
            {
                "value": "saturated",
                "label": "饱和型",
                "summary": "压力较低时响应明显，压力升高后增量逐步饱和。",
                "effect": "避免高压力区间继续按同样斜率无限放大。",
            },
            {
                "value": "batterydrain",
                "label": "电量牵引型",
                "summary": "压力响应同时更受剩余精力影响。",
                "effect": "精力偏低时，同样任务可能产生更强的压力响应。",
            },
        ],
    },
    "C_strategy": {
        "label": "连续负荷策略",
        "short_label": "连续负荷",
        "description": "决定连续工作达到多久后开始出现额外疲劳惩罚，以及惩罚如何增长。",
        "choices": [
            {
                "value": "high",
                "label": "高累积型",
                "summary": "较早进入连续负荷惩罚，后段增长更明显。",
                "effect": "对长时间连轴转更敏感。",
            },
            {
                "value": "threshold",
                "label": "阈值型",
                "summary": "阈值前基本不增加，超过后平滑进入惩罚。",
                "effect": "适合存在较清晰耐受时长的情况。",
            },
            {
                "value": "low",
                "label": "低累积型",
                "summary": "触发更晚、额外惩罚增长更缓。",
                "effect": "对连续工作时长的附加影响较弱。",
            },
        ],
    },
    "night_strategy": {
        "label": "夜间恢复策略",
        "short_label": "夜间恢复",
        "description": "决定睡眠阶段压力回落、节律波动与精力恢复的形态。",
        "choices": [
            {
                "value": "normal",
                "label": "平稳型",
                "summary": "按标准节律逐步回落压力并恢复精力。",
                "effect": "作为默认的夜间恢复基线。",
            },
            {
                "value": "deep",
                "label": "深度型",
                "summary": "压力稳定更快，精力恢复倍率更高。",
                "effect": "夜间曲线整体更偏向充分恢复。",
            },
            {
                "value": "anxious",
                "label": "波动型",
                "summary": "保留更明显的夜间压力波动，恢复上限较保守。",
                "effect": "适合描述睡眠中仍较难完全放松的走势。",
            },
        ],
    },
    "rest_strategy": {
        "label": "休息恢复策略",
        "short_label": "休息方式",
        "description": "决定白天空闲或休息时，压力下降和精力恢复随休息时长如何变化。",
        "choices": [
            {
                "value": "relieved",
                "label": "释然型",
                "summary": "进入休息后较快获得减压和恢复。",
                "effect": "短休息也能较快产生效果。",
            },
            {
                "value": "warmup",
                "label": "慢热型",
                "summary": "开始阶段效果有限，休息持续后恢复斜率提升。",
                "effect": "更需要完整、不被打断的休息时间。",
            },
            {
                "value": "anxious",
                "label": "难放松型",
                "summary": "高压时减压启动较慢，之后逐步改善。",
                "effect": "短暂空闲不一定马上转化为恢复。",
            },
            {
                "value": "burnout",
                "label": "倦怠型",
                "summary": "压力下降和精力恢复都较弱，长休息也可能出现耗散。",
                "effect": "用于保守模拟恢复效率明显偏低的阶段。",
            },
        ],
    },
}

FAMILY_ORDER = tuple(STRATEGY_CATALOG.keys())


def _choice_values(family: str) -> set[str]:
    return {
        str(item["value"])
        for item in STRATEGY_CATALOG[family]["choices"]
    }


def validate_strategy_selection(value: Any) -> Dict[str, str]:
    """Validate a partial, strategy-only update."""
    if not isinstance(value, dict) or not value:
        raise ValueError("strategies must be a non-empty object")
    unknown = sorted(set(value) - set(FAMILY_ORDER))
    if unknown:
        raise ValueError(f"unsupported strategy fields: {', '.join(unknown)}")

    normalized: Dict[str, str] = {}
    for family, selected in value.items():
        candidate = str(selected or "").strip().lower()
        if candidate not in _choice_values(family):
            raise ValueError(f"unsupported {family}: {candidate or '(empty)'}")
        normalized[family] = candidate
    return normalized


def strategy_payload(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return descriptions plus the current strategy values."""
    catalog = deepcopy(STRATEGY_CATALOG)
    current = {}
    for family in FAMILY_ORDER:
        allowed = _choice_values(family)
        selected = str(params.get(family) or "").lower()
        if selected not in allowed:
            selected = str(catalog[family]["choices"][0]["value"])
        current[family] = selected
        catalog[family]["current"] = selected
    return {
        "schema_version": "strategy_preferences.v1",
        "current": current,
        "families": [catalog[key] | {"key": key} for key in FAMILY_ORDER],
        "notice": "历史 S/E 引擎诊断数据；这些离散类型不进入 CTSSM，也不再向用户开放。",
    }


def _bounded_number(
    value: Any,
    default: float,
    lower: float,
    upper: float,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(lower, min(upper, number))


def build_strategy_curves(
    params: Dict[str, Any],
    family: str,
    *,
    stress: Any = 65.0,
    energy: Any = 55.0,
    baseline: Any = 50.0,
) -> Dict[str, Any]:
    """Evaluate the real strategy implementations without random noise."""
    if family not in STRATEGY_CATALOG:
        raise ValueError("unsupported strategy family")

    stress_value = _bounded_number(stress, 65.0, 0.0, 100.0)
    energy_value = _bounded_number(energy, 55.0, 0.0, 100.0)
    baseline_value = _bounded_number(baseline, 50.0, 0.0, 100.0)
    safe_params = deepcopy(params)
    safe_params["S_star_init"] = baseline_value
    safe_params["random_seed"] = int(safe_params.get("random_seed", 42))

    catalog = STRATEGY_CATALOG[family]
    series = []
    metrics = []
    x_axis = {}

    for choice in catalog["choices"]:
        strategy_name = str(choice["value"])
        points = []
        trace = ""

        if family == "f_strategy":
            x_axis = {"key": "stress", "label": "当前压力", "unit": "分"}
            metrics = [
                {"key": "response", "label": "压力响应因子"},
                {"key": "allostatic_amplifier", "label": "低精力放大器"},
                {"key": "energy_drain_modifier", "label": "精力消耗倍率"},
            ]
            strategy = make_f_strategy(strategy_name, safe_params)
            for current_stress in range(0, 101, 5):
                response = strategy.f_s(
                    float(current_stress),
                    energy_value,
                    baseline_value,
                    step_noise_s=0.0,
                )
                points.append(
                    {
                        "x": float(current_stress),
                        "response": round(float(response), 6),
                        "allostatic_amplifier": round(
                            float(strategy.get_allostatic_stress_amplifier(energy_value)),
                            6,
                        ),
                        "energy_drain_modifier": round(
                            float(strategy.get_energy_drain_modifier(energy_value)),
                            6,
                        ),
                    }
                )
            trace = getattr(strategy, "last_trace", "")

        elif family == "C_strategy":
            x_axis = {"key": "continuous_hours", "label": "连续负荷", "unit": "小时"}
            metrics = [{"key": "penalty", "label": "单步疲劳惩罚"}]
            strategy = make_C_strategy(strategy_name, safe_params)
            for index in range(0, 33):
                hours = index * 0.25
                penalty = strategy.calculate_fatigue_penalty(hours, baseline_value)
                points.append({"x": hours, "penalty": round(float(penalty), 6)})
            trace = getattr(strategy, "last_penalty_trace", "")

        elif family == "rest_strategy":
            x_axis = {"key": "rest_minutes", "label": "休息持续时间", "unit": "分钟"}
            metrics = [
                {"key": "delta_s", "label": "单步压力变化"},
                {"key": "delta_e", "label": "单步精力变化"},
            ]
            strategy = make_rest_strategy(strategy_name, safe_params)
            for minutes in range(0, 121, 5):
                delta_s, delta_e = strategy.calculate_flow_recovery(
                    stress_value,
                    energy_value,
                    float(minutes),
                    5,
                    baseline_value,
                    step_noise_s=0.0,
                    step_noise_e=0.0,
                )
                points.append(
                    {
                        "x": float(minutes),
                        "delta_s": round(float(delta_s), 6),
                        "delta_e": round(float(delta_e), 6),
                    }
                )
            trace = getattr(strategy, "last_trace", "")

        else:
            x_axis = {"key": "sleep_minutes", "label": "入睡后时间", "unit": "分钟"}
            metrics = [
                {"key": "delta_s", "label": "单步压力变化"},
                {"key": "delta_e", "label": "单步精力变化"},
            ]
            strategy = make_night_strategy(strategy_name, safe_params)
            start = datetime(2026, 1, 1, 23, 0)
            for minutes in range(0, 481, 10):
                delta_s, delta_e = strategy.calculate_step(
                    stress_value,
                    energy_value,
                    start + timedelta(minutes=minutes),
                    5,
                    elapsed_minutes=float(minutes),
                    step_noise_s=0.0,
                    step_noise_e=0.0,
                )
                points.append(
                    {
                        "x": float(minutes),
                        "delta_s": round(float(delta_s), 6),
                        "delta_e": round(float(delta_e), 6),
                    }
                )

        series.append(
            {
                "id": strategy_name,
                "label": choice["label"],
                "summary": choice["summary"],
                "points": points,
                "trace": trace,
            }
        )

    return {
        "schema_version": "strategy_curve_diagnostic.v1",
        "family": family,
        "label": catalog["label"],
        "description": catalog["description"],
        "x_axis": x_axis,
        "metrics": metrics,
        "series": series,
        "inputs": {
            "stress": stress_value,
            "energy": energy_value,
            "baseline": baseline_value,
            "noise": 0.0,
            "time_step_minutes": 5,
        },
        "assumptions": [
            "使用当前参数版本的真实策略函数。",
            "随机噪声固定为 0，便于比较函数本身的形状。",
            "曲线用于开发诊断，不代表医学测量或个体真实状态。",
        ],
    }
