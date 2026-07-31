"""Versioned onboarding questionnaire and deterministic profile inference.

The questionnaire is intentionally small and replaceable. Raw answers are kept
separate from the mapping rules so future question edits do not silently change
historical profile snapshots.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
import uuid


QUESTIONNAIRE_VERSION = "2026-07-30.v1"
MAPPING_VERSION = "profile_mapping.v1"
QUESTIONNAIRE_SCHEMA_VERSION = "questionnaire_definition.v1"
ONBOARDING_SCHEMA_VERSION = "onboarding.v1"
MODEL_VERSION = "se-baseline.v1"
PARAMETER_VERSION = "phase0-defaults.v1"
FEATURE_VERSION = "event_features.v1"


QUESTIONNAIRE_DEFINITION: Dict[str, Any] = {
    "questionnaire_version": QUESTIONNAIRE_VERSION,
    "schema_version": QUESTIONNAIRE_SCHEMA_VERSION,
    "title": "认识你的日常节律",
    "description": "大约 3 分钟。答案只用于建立初始偏好，不是心理诊断。",
    "sections": [
        {
            "section_id": "routine",
            "title": "日常节律",
            "eyebrow": "01 · 作息",
            "description": "先告诉我们你理想中的一天。之后仍可随时修改。",
            "questions": [
                {
                    "question_id": "weekday_sleep_start",
                    "prompt": "工作日通常几点准备入睡？",
                    "response_type": "local_time",
                    "required": True,
                    "default": "23:30",
                    "direct_config_target": "routine.weekday_sleep_start",
                    "construct_mappings": [],
                },
                {
                    "question_id": "weekday_wake_time",
                    "prompt": "工作日通常几点起床？",
                    "response_type": "local_time",
                    "required": True,
                    "default": "07:30",
                    "direct_config_target": "routine.weekday_wake_time",
                    "construct_mappings": [],
                },
                {
                    "question_id": "lunch_ideal_time",
                    "prompt": "你希望几点吃午餐？",
                    "response_type": "local_time",
                    "required": True,
                    "default": "12:10",
                    "direct_config_target": "routine.lunch_ideal_time",
                    "construct_mappings": [],
                },
                {
                    "question_id": "dinner_ideal_time",
                    "prompt": "你希望几点吃晚餐？",
                    "response_type": "local_time",
                    "required": True,
                    "default": "18:20",
                    "direct_config_target": "routine.dinner_ideal_time",
                    "construct_mappings": [],
                },
                {
                    "question_id": "nap_frequency",
                    "prompt": "你通常会午睡吗？",
                    "response_type": "single_choice",
                    "required": True,
                    "default": "sometimes",
                    "options": [
                        {"value": "never", "label": "几乎不"},
                        {"value": "sometimes", "label": "偶尔"},
                        {"value": "often", "label": "经常"},
                    ],
                    "direct_config_target": "routine.nap_frequency",
                    "construct_mappings": [],
                },
            ],
        },
        {
            "section_id": "response",
            "title": "压力与恢复",
            "eyebrow": "02 · 感受",
            "description": "按最近一个月的通常感受作答，没有“正确答案”。",
            "questions": [
                {
                    "question_id": "stress_change_01",
                    "prompt": "临时改变计划会让我明显不安。",
                    "response_type": "likert_1_5",
                    "required": True,
                    "construct_mappings": [
                        {"construct": "uncertainty_sensitivity", "polarity": "forward", "weight": 1.0}
                    ],
                },
                {
                    "question_id": "adapt_change_reverse_01",
                    "prompt": "计划变化后，我通常能很快重新进入状态。",
                    "response_type": "likert_1_5",
                    "required": True,
                    "construct_mappings": [
                        {"construct": "uncertainty_sensitivity", "polarity": "reverse", "weight": 0.8},
                        {"construct": "recovery_capacity", "polarity": "forward", "weight": 0.5},
                    ],
                },
                {
                    "question_id": "recovery_speed_01",
                    "prompt": "忙碌结束后，我需要较长时间才能放松下来。",
                    "response_type": "likert_1_5",
                    "required": True,
                    "construct_mappings": [
                        {"construct": "recovery_capacity", "polarity": "reverse", "weight": 1.0}
                    ],
                },
                {
                    "question_id": "continuous_load_01",
                    "prompt": "连续处理多项任务时，我会很快感到精力被耗尽。",
                    "response_type": "likert_1_5",
                    "required": True,
                    "construct_mappings": [
                        {"construct": "load_sensitivity", "polarity": "forward", "weight": 1.0}
                    ],
                },
                {
                    "question_id": "morning_energy_reverse_01",
                    "prompt": "睡眠充足时，我早上通常精力不错。",
                    "response_type": "likert_1_5",
                    "required": True,
                    "construct_mappings": [
                        {"construct": "load_sensitivity", "polarity": "reverse", "weight": 0.5},
                        {"construct": "recovery_capacity", "polarity": "forward", "weight": 0.7},
                    ],
                },
                {
                    "question_id": "social_evaluation_01",
                    "prompt": "汇报、答辩或被评价的场景会让我更紧绷。",
                    "response_type": "likert_1_5",
                    "required": True,
                    "construct_mappings": [
                        {"construct": "evaluation_sensitivity", "polarity": "forward", "weight": 1.0}
                    ],
                },
            ],
        },
        {
            "section_id": "care",
            "title": "支持偏好",
            "eyebrow": "03 · 关怀",
            "description": "你决定系统何时出现、怎样说话。",
            "questions": [
                {
                    "question_id": "support_style",
                    "prompt": "压力升高时，你更希望先收到哪类帮助？",
                    "response_type": "multiple_choice",
                    "required": True,
                    "options": [
                        {"value": "task_breakdown", "label": "拆小任务"},
                        {"value": "short_break", "label": "短暂休息"},
                        {"value": "breathing", "label": "呼吸放松"},
                        {"value": "quiet_companionship", "label": "安静陪伴"},
                    ],
                    "construct_mappings": [],
                },
                {
                    "question_id": "care_tone",
                    "prompt": "你喜欢怎样的提醒语气？",
                    "response_type": "single_choice",
                    "required": True,
                    "default": "brief_warm",
                    "options": [
                        {"value": "brief_warm", "label": "简短温和"},
                        {"value": "calm_practical", "label": "冷静实用"},
                        {"value": "minimal", "label": "只说重点"},
                    ],
                    "construct_mappings": [],
                },
                {
                    "question_id": "change_experience_text",
                    "prompt": "还有什么希望系统了解？（选填）",
                    "help": "例如：临时换课后，我通常需要一段时间才能重新进入状态。",
                    "response_type": "optional_text",
                    "required": False,
                    "construct_mappings": [],
                },
            ],
        },
    ],
    "scale_labels": {
        "1": "完全不符合",
        "2": "不太符合",
        "3": "一般",
        "4": "比较符合",
        "5": "非常符合",
    },
    "parameter_whitelist": [
        "K_resilience",
        "fatigue_acceleration",
        "event_sensitivity.uncertainty",
        "event_sensitivity.evaluation",
    ],
}


TRAIT_LABELS = {
    "uncertainty_sensitivity": "变化敏感度",
    "recovery_capacity": "恢复能力",
    "load_sensitivity": "连续负荷敏感度",
    "evaluation_sensitivity": "评价场景敏感度",
}


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iter_questions() -> Iterable[Dict[str, Any]]:
    for section in QUESTIONNAIRE_DEFINITION["sections"]:
        yield from section["questions"]


def validate_and_normalize_submission(payload: Dict[str, Any]) -> Dict[str, Any]:
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object")

    normalized: Dict[str, Any] = {}
    errors: List[str] = []
    for question in iter_questions():
        question_id = question["question_id"]
        value = answers.get(question_id, question.get("default"))
        response_type = question["response_type"]
        if question.get("required") and (value is None or value == "" or value == []):
            errors.append(f"{question_id} is required")
            continue
        if value in (None, "") and not question.get("required"):
            normalized[question_id] = ""
            continue
        if response_type == "likert_1_5":
            try:
                value = int(value)
            except (TypeError, ValueError):
                errors.append(f"{question_id} must be an integer")
                continue
            if value < 1 or value > 5:
                errors.append(f"{question_id} must be between 1 and 5")
                continue
        elif response_type == "local_time":
            if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
                errors.append(f"{question_id} must use HH:MM")
                continue
            try:
                hour, minute = (int(part) for part in value.split(":"))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    raise ValueError
            except ValueError:
                errors.append(f"{question_id} must use a valid local time")
                continue
        elif response_type in {"single_choice", "multiple_choice"}:
            allowed = {option["value"] for option in question.get("options", [])}
            values = value if isinstance(value, list) else [value]
            if not values or any(item not in allowed for item in values):
                errors.append(f"{question_id} contains an unsupported choice")
                continue
            if response_type == "single_choice":
                value = values[0]
            else:
                value = list(dict.fromkeys(values))
        elif response_type == "optional_text":
            value = str(value).strip()[:1000]
        normalized[question_id] = value

    if errors:
        raise ValueError("; ".join(errors))

    timezone_name = str(payload.get("timezone") or "Asia/Shanghai")[:64]
    return {
        "response_id": new_id(),
        "schema_version": ONBOARDING_SCHEMA_VERSION,
        "questionnaire_version": QUESTIONNAIRE_VERSION,
        "submitted_at": utc_now(),
        "timezone": timezone_name,
        "answers": normalized,
    }


def infer_profile(response: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    answers = response["answers"]
    accumulators: Dict[str, List[Tuple[float, float, str, str]]] = {}
    for question in iter_questions():
        value = answers.get(question["question_id"])
        if question["response_type"] != "likert_1_5" or value is None:
            continue
        normalized = (int(value) - 1) / 4.0
        for mapping in question.get("construct_mappings", []):
            mapped_value = 1.0 - normalized if mapping["polarity"] == "reverse" else normalized
            accumulators.setdefault(mapping["construct"], []).append(
                (mapped_value, float(mapping["weight"]), question["question_id"], mapping["polarity"])
            )

    traits = []
    for trait_name, entries in accumulators.items():
        weight_sum = sum(entry[1] for entry in entries)
        score = sum(entry[0] * entry[1] for entry in entries) / weight_sum
        coverage = min(1.0, len(entries) / 2.0)
        spread = max(entry[0] for entry in entries) - min(entry[0] for entry in entries)
        confidence = max(0.45, min(0.95, 0.58 + coverage * 0.27 - spread * 0.12))
        traits.append(
            {
                "trait": trait_name,
                "label": TRAIT_LABELS[trait_name],
                "score_0_1": round(score, 3),
                "confidence_0_1": round(confidence, 3),
                "evidence": [
                    {
                        "question_id": question_id,
                        "normalized_value": round(value, 3),
                        "role": f"{polarity}_item",
                    }
                    for value, _, question_id, polarity in entries
                ],
                "quality_flags": [],
            }
        )

    by_trait = {item["trait"]: item["score_0_1"] for item in traits}
    recovery = by_trait.get("recovery_capacity", 0.5)
    load = by_trait.get("load_sensitivity", 0.5)
    uncertainty = by_trait.get("uncertainty_sensitivity", 0.5)
    evaluation = by_trait.get("evaluation_sensitivity", 0.5)
    parameter_priors = [
        _prior("K_resilience", 0.75 + recovery * 0.5, 0.65, 1.35, ["recovery_capacity"]),
        _prior("fatigue_acceleration", 0.10 + load * 0.12, 0.08, 0.25, ["load_sensitivity"]),
        _prior(
            "event_sensitivity.uncertainty",
            0.8 + uncertainty * 0.45,
            0.75,
            1.35,
            ["uncertainty_sensitivity"],
        ),
        _prior(
            "event_sensitivity.evaluation",
            0.8 + evaluation * 0.45,
            0.75,
            1.35,
            ["evaluation_sensitivity"],
        ),
    ]

    inference_run = {
        "profile_inference_run_id": new_id(),
        "schema_version": "profile_inference.v1",
        "response_id": response["response_id"],
        "mapping_version": MAPPING_VERSION,
        "created_at": utc_now(),
        "semantic_processor": {
            "mode": "rules",
            "model": None,
            "prompt_version": None,
            "fallback_available": True,
        },
        "traits": traits,
        "parameter_priors": parameter_priors,
        "global_quality": {
            "status": "valid",
            "missing_ratio": 0.0,
            "consistency_score_0_1": round(
                max(0.5, 0.9 - abs(uncertainty - (1.0 - recovery)) * 0.25),
                3,
            ),
        },
    }

    routine = {
        "weekday_sleep_start": answers["weekday_sleep_start"],
        "weekday_wake_time": answers["weekday_wake_time"],
        "lunch_ideal_time": answers["lunch_ideal_time"],
        "lunch_allowed_window": _window(answers["lunch_ideal_time"], 50, 70),
        "dinner_ideal_time": answers["dinner_ideal_time"],
        "dinner_allowed_window": _window(answers["dinner_ideal_time"], 80, 100),
        "nap_frequency": answers["nap_frequency"],
        "nap_window": ["12:20", "14:00"],
    }
    care_preferences = {
        "enabled": True,
        "quiet_hours": ["23:00", "07:00"],
        "max_daily_messages": 2,
        "tone": answers["care_tone"],
        "preferred_support": answers["support_style"],
        "allow_personal_history_reference": False,
        "allow_external_llm": False,
    }
    profile_snapshot = {
        "profile_snapshot_id": new_id(),
        "schema_version": "profile_snapshot.v1",
        "profile_inference_run_id": inference_run["profile_inference_run_id"],
        "mapping_version": MAPPING_VERSION,
        "created_at": utc_now(),
        "expires_at": None,
        "traits": traits,
        "parameter_priors": parameter_priors,
        "routine": routine,
        "care_preferences": care_preferences,
        "summary": _profile_summary(by_trait),
    }
    return inference_run, profile_snapshot


def build_routine_plan(
    profile_snapshot: Dict[str, Any],
    target_date: Optional[str] = None,
    occupied_windows: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    target_date = target_date or date.today().isoformat()
    routine = profile_snapshot["routine"]
    occupied = occupied_windows or []
    items = []
    items.append(
        _schedule_routine(
            "lunch",
            routine["lunch_ideal_time"],
            routine["lunch_allowed_window"],
            30,
            occupied,
        )
    )
    if routine["nap_frequency"] == "never":
        items.append(
            {
                "routine_type": "nap",
                "ideal_window": routine["nap_window"],
                "allowed_window": routine["nap_window"],
                "scheduled_window": None,
                "status": "not_expected",
                "source": "questionnaire",
                "confidence": 0.92,
                "reason_codes": ["user_does_not_usually_nap"],
            }
        )
    else:
        nap_duration = 30 if routine["nap_frequency"] == "often" else 20
        items.append(
            _schedule_routine(
                "nap",
                "13:00",
                routine["nap_window"],
                nap_duration,
                occupied + [
                    {"start": item["scheduled_window"][0], "end": item["scheduled_window"][1]}
                    for item in items
                    if item["scheduled_window"]
                ],
            )
        )
    items.append(
        _schedule_routine(
            "dinner",
            routine["dinner_ideal_time"],
            routine["dinner_allowed_window"],
            30,
            occupied,
        )
    )
    return {
        "routine_plan_id": new_id(),
        "schema_version": "routine_plan.v1",
        "local_date": target_date,
        "profile_snapshot_id": profile_snapshot["profile_snapshot_id"],
        "rule_version": "routine_weaver.v2",
        "created_at": utc_now(),
        "items": items,
    }


def build_daily_context(
    profile_snapshot: Dict[str, Any],
    routine_plan: Dict[str, Any],
    target_date: str,
) -> Dict[str, Any]:
    return {
        "context_snapshot_id": new_id(),
        "schema_version": "daily_context.v1",
        "target_date": target_date,
        "created_at": utc_now(),
        "profile_snapshot_id": profile_snapshot["profile_snapshot_id"],
        "previous_day": None,
        "recent_7d": None,
        "routine_plan_id": routine_plan["routine_plan_id"],
        "data_quality": {"observed_ratio": 0.0, "imputed_ratio": 1.0},
    }


def _prior(
    parameter: str,
    mean: float,
    lower_bound: float,
    upper_bound: float,
    source_traits: List[str],
) -> Dict[str, Any]:
    mean = max(lower_bound, min(upper_bound, mean))
    half_width = (upper_bound - lower_bound) * 0.2
    return {
        "parameter": parameter,
        "mean": round(mean, 3),
        "lower": round(max(lower_bound, mean - half_width), 3),
        "upper": round(min(upper_bound, mean + half_width), 3),
        "prior_strength": "weak",
        "source_traits": source_traits,
    }


def _window(value: str, before_minutes: int, after_minutes: int) -> List[str]:
    total = _to_minutes(value)
    return [_to_time(total - before_minutes), _to_time(total + after_minutes)]


def _to_minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def _to_time(value: int) -> str:
    value %= 24 * 60
    return f"{value // 60:02d}:{value % 60:02d}"


def _overlaps(start: int, end: int, occupied: List[Dict[str, str]]) -> bool:
    return any(start < _to_minutes(item["end"]) and end > _to_minutes(item["start"]) for item in occupied)


def _schedule_routine(
    routine_type: str,
    ideal_time: str,
    allowed_window: List[str],
    duration: int,
    occupied: List[Dict[str, str]],
) -> Dict[str, Any]:
    allowed_start, allowed_end = map(_to_minutes, allowed_window)
    ideal_start = max(allowed_start, min(_to_minutes(ideal_time), allowed_end - duration))
    candidates = [ideal_start]
    for offset in range(10, max(10, allowed_end - allowed_start + 10), 10):
        candidates.extend([ideal_start - offset, ideal_start + offset])

    selected = None
    for candidate in candidates:
        if candidate < allowed_start or candidate + duration > allowed_end:
            continue
        if not _overlaps(candidate, candidate + duration, occupied):
            selected = candidate
            break

    if selected is None:
        return {
            "routine_type": routine_type,
            "ideal_window": [ideal_time, _to_time(_to_minutes(ideal_time) + duration)],
            "allowed_window": allowed_window,
            "scheduled_window": None,
            "status": "unavailable",
            "source": "questionnaire_imputed",
            "confidence": 0.72,
            "reason_codes": ["allowed_window_fully_occupied"],
        }
    shifted = selected != ideal_start
    return {
        "routine_type": routine_type,
        "ideal_window": [ideal_time, _to_time(_to_minutes(ideal_time) + duration)],
        "allowed_window": allowed_window,
        "scheduled_window": [_to_time(selected), _to_time(selected + duration)],
        "status": "shifted" if shifted else "scheduled",
        "source": "questionnaire_imputed",
        "confidence": 0.86,
        "reason_codes": ["ideal_time_occupied"] if shifted else [],
    }


def _profile_summary(traits: Dict[str, float]) -> str:
    strengths = []
    if traits.get("recovery_capacity", 0.5) >= 0.62:
        strengths.append("恢复节律较稳定")
    if traits.get("uncertainty_sensitivity", 0.5) >= 0.62:
        strengths.append("对临时变化更敏感")
    if traits.get("load_sensitivity", 0.5) >= 0.62:
        strengths.append("连续任务更容易消耗精力")
    if traits.get("evaluation_sensitivity", 0.5) >= 0.62:
        strengths.append("评价场景需要更多准备空间")
    if not strengths:
        return "当前画像较为均衡，系统会通过后续轻量反馈继续校准。"
    return "；".join(strengths) + "。这只是初始偏好画像，会随你的反馈逐步校准。"
