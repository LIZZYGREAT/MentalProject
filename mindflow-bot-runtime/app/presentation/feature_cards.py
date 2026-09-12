"""Single-source reviewed feature discovery cards.

Every user-facing feature overview/detail surface renders from these specs.
Callback values carry only ``feature_key`` and ``version``; the feature key is
validated against this backend enum, never taken from free text. Features whose
capability flag is disabled are hidden instead of shown with a dead button.
"""

from __future__ import annotations

from typing import Any, Mapping


OVERVIEW_FEATURE_KEY = "overview"

FEATURE_SPECS: dict[str, dict[str, Any]] = {
    "checkin": {
        "title": "状态记录",
        "summary": "记录此刻的压力和精力，慢慢攒出你自己的状态曲线。",
        "what": (
            "告诉我你现在的压力和精力（0 到 10 分）以及正在做什么，"
            "我帮你记下来；也可以发一张填写卡，点选就可以。"
        ),
        "how_to_say": (
            "记一下，我现在压力 6、精力 5",
            "给我个表填状态",
        ),
        "limits": ("状态记录由你主动发起，我不会替你猜测或自动填写。",),
        "capability_flag": None,
    },
    "pressure_curve": {
        "title": "压力曲线",
        "summary": "基于你的记录和日程，给出今天或明天的压力趋势预估。",
        "what": (
            "把你的记录和研究者的模型结果画成一张曲线卡，"
            "可以看今天、明天，或已经生成过的历史预估。"
        ),
        "how_to_say": (
            "看看今天的压力曲线",
            "看看明天的压力曲线",
        ),
        "limits": (
            "曲线是模型预估，不是事实，也不是诊断。",
            "记录还太少的时候，我会直接说明，而不是硬给一个结果。",
        ),
        "capability_flag": None,
    },
    "calendar": {
        "title": "日程与日历",
        "summary": "查看、添加、修改或删除你的飞书日历日程。",
        "what": (
            "可以问今天、明天或某一周的安排，也可以让我添加、修改或删除具体的日程。"
            "添加、修改或删除都需要你明确提出；删除、批量等需要确认的操作会按固定流程确认。"
        ),
        "how_to_say": (
            "明天有什么安排",
            "周四下午三点加一个组会",
        ),
        "limits": (
            "第一次使用日历需要发 /calendar 完成一次授权。",
        ),
        "capability_flag": None,
    },
    "course_schedule": {
        "title": "课程表导入",
        "summary": "发一张课程表图片，识别后先确认，再按你的选择加进日历。",
        "what": (
            "我会先识别图片里的课程、时间和周次，生成一份预览；"
            "你确认无误并选择重复方式后，才会写入日历。"
        ),
        "how_to_say": (
            "直接发送课程表图片",
            "导入这张课表",
        ),
        "limits": (
            "一次最多导入 20 门课，课程太多可以分两张图发。",
            "确认之前不会写入任何日历数据。",
        ),
        "capability_flag": None,
    },
    "daily_review": {
        "title": "每日回顾",
        "summary": "一天结束时用三个小问题记录：开始、峰值和结束时的状态。",
        "what": (
            "每天晚上可以收到一张回顾卡，用几秒钟点选一天的状态；"
            "记录会汇入你的状态曲线。"
        ),
        "how_to_say": (
            "现在开始每日回顾",
            "帮我设置每天的回顾提醒",
        ),
        "limits": ("默认不开启，需要你明确让我设置或直接发起。",),
        "capability_flag": "daily_review_enabled",
    },
    "care_alerts": {
        "title": "关怀提醒",
        "summary": "在压力可能升高的时段，收到一条可以随时忽略的小提示。",
        "what": (
            "提醒基于你的记录和日程趋势，只在你可能需要的时候出现；"
            "内容都是经过审核的固定文案。"
        ),
        "how_to_say": (
            "最近提醒有点多，帮我减少提醒",
            "今天别提醒我了",
        ),
        "limits": ("可以随时关闭某一类提醒，或设置静默时间段。",),
        "capability_flag": None,
    },
    "data_privacy": {
        "title": "数据与隐私",
        "summary": "查看 MindFlow 如何使用和保护你的数据。",
        "what": (
            "MindFlow 会把状态记录、日历数据和模型结果按不同用途处理。"
            "系统内部使用参与者编号关联记录，并限制工具和模型获得不必要的身份信息。"
        ),
        "how_to_say": (
            "我的数据是怎么保存的",
            "谁可以看到我的记录",
        ),
        "limits": (
            "具体的数据使用、保存、研究用途和删除方式，以项目正式说明与知情同意内容为准。",
        ),
        "capability_flag": None,
    },
}


def visible_feature_keys(
    capabilities: Mapping[str, bool] | None = None,
) -> tuple[str, ...]:
    """Feature keys whose capability flag is enabled, in stable order."""

    caps = dict(capabilities or {})
    return tuple(
        key
        for key, spec in FEATURE_SPECS.items()
        if not spec.get("capability_flag") or bool(caps.get(spec["capability_flag"], True))
    )


def normalize_feature_key(value: Any) -> str | None:
    """Backend enum for card callback feature keys; never trust raw text."""

    key = str(value or "").strip()
    if key == OVERVIEW_FEATURE_KEY or key in FEATURE_SPECS:
        return key
    return None


def _feature_button(
    action_name: str, feature_key: str, text: str, *, primary: bool = False
) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "primary" if primary else "default",
        "text": {"tag": "plain_text", "content": text},
        "behaviors": [{"type": "callback", "value": {
            "mindflow_action": action_name,
            "version": "1",
            "feature_key": feature_key,
        }}],
    }


def _feature_card(
    title: str, content: str, elements: list[dict[str, Any]]
) -> dict[str, Any]:
    elements.insert(0, {"tag": "markdown", "content": content})
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "enable_forward": False},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"direction": "vertical", "elements": elements},
    }


def feature_overview_card(feature_keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    keys = feature_keys or visible_feature_keys()
    lines = ["这些是我目前能帮你做的事情：", ""]
    for key in keys:
        spec = FEATURE_SPECS.get(key)
        if spec is None:
            continue
        lines.append(f"**{spec['title']}** · {spec['summary']}")
        lines.append("")
    lines.append("点下面的按钮可以看每一项的用法。")
    elements = [_feature_button("feature_open", key, FEATURE_SPECS[key]["title"])
                for key in keys if key in FEATURE_SPECS]
    return _feature_card("MindFlow 功能一览", "\n".join(lines).rstrip(), elements)


def feature_overview_text(feature_keys: tuple[str, ...] | None = None) -> str:
    """Plain-text overview rendered from the same specs as the overview card.

    Used when the interactive card cannot be delivered, so disabled features
    stay hidden in every fallback surface.
    """

    keys = feature_keys or visible_feature_keys()
    lines = ["目前可以用这些：", ""]
    for key in keys:
        spec = FEATURE_SPECS.get(key)
        if spec is None:
            continue
        lines.append(f"• {spec['title']}：{spec['summary']}")
    lines.append("")
    lines.append("发“功能”可以随时再看一次；想了解某一项的用法，直接问我就行。")
    return "\n".join(lines)


def feature_detail_card(
    feature_key: str, feature_keys: tuple[str, ...] | None = None
) -> dict[str, Any] | None:
    key = normalize_feature_key(feature_key)
    if key is None or key == OVERVIEW_FEATURE_KEY:
        return None
    spec = FEATURE_SPECS[key]
    keys = feature_keys or visible_feature_keys()
    if key not in keys:
        return None
    lines = [f"**{spec['title']}** · {spec['summary']}", "", "它是什么", spec["what"], ""]
    lines.append("可以这样说")
    lines.extend(f"- {example}" for example in spec["how_to_say"])
    lines.append("")
    lines.append("需要知道的")
    lines.extend(f"- {limit}" for limit in spec["limits"])
    elements: list[dict[str, Any]] = []
    if key == "data_privacy":
        # Participant-bound settings entry; renders the per-participant
        # external AI processing status card through the fixed backend flow.
        elements.append({
            "tag": "button",
            "type": "primary",
            "text": {"tag": "plain_text", "content": "外部 AI 处理设置"},
            "behaviors": [{"type": "callback", "value": {
                "mindflow_action": "external_llm_consent_status_open",
                "version": "1",
            }}],
        })
    elements.append(_feature_button("feature_back", OVERVIEW_FEATURE_KEY, "返回全部功能"))
    return _feature_card(f"功能 · {spec['title']}", "\n".join(lines), elements)


def build_feature_card(
    feature_key: str, feature_keys: tuple[str, ...] | None = None
) -> dict[str, Any] | None:
    """Render one fixed card for a validated feature key or the overview."""

    key = normalize_feature_key(feature_key)
    if key is None:
        return None
    if key == OVERVIEW_FEATURE_KEY:
        return feature_overview_card(feature_keys)
    return feature_detail_card(key, feature_keys)


def onboarding_welcome_card(
    feature_keys: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Progressive first screen after binding; buttons only navigate.

    The first three buttons open one feature's usage guidance; none of them
    performs a state-changing operation directly.
    """

    keys = feature_keys or visible_feature_keys()
    elements: list[dict[str, Any]] = []
    for wanted in ("checkin", "calendar", "course_schedule"):
        if wanted in keys:
            elements.append(_feature_button(
                "feature_open",
                wanted,
                FEATURE_SPECS[wanted]["title"],
                primary=(wanted == "checkin"),
            ))
    elements.append(_feature_button(
        "feature_open", OVERVIEW_FEATURE_KEY, "全部功能"
    ))
    return _feature_card(
        "欢迎使用 MindFlow",
        "绑定好了 🌱 不用一次了解全部，可以先从一件事开始：",
        elements,
    )
