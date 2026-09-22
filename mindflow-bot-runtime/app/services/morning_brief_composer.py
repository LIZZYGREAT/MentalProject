"""Compose a factual morning brief from personal facts and evidence only."""

from __future__ import annotations

from typing import Any, Iterable

from app.contracts.research import ResearchEvidenceItem


FORBIDDEN_BRIEF_TERMS = frozenset({"压力预测", "压力等级", "高压峰值", "AUC", "风险窗口", "模型解释"})


class MorningBriefComposer:
    @staticmethod
    def _summary(item: ResearchEvidenceItem, topic: str) -> tuple[str, str]:
        text = " ".join(item.content.split())
        if not text:
            return "正文暂未提供可提取摘要。", f"与“{topic}”主题相关，建议打开来源核实。"
        sentence = text.split("。", 1)[0].split(".", 1)[0].strip()
        summary = (sentence or text)[:220]
        return summary, f"与“{topic}”主题相关，摘要来自已读取的公开正文。"

    def compose(
        self,
        local_date: str,
        events: list[dict[str, Any]],
        reminders: list[dict[str, Any]],
        research_by_topic: dict[str, Iterable[ResearchEvidenceItem | dict[str, Any]]] | None = None,
        *,
        include_calendar: bool = True,
        include_reminders: bool = True,
        research_unavailable: bool = False,
    ) -> str:
        lines = [f"早上好，今天是 {local_date}。", ""]
        if include_calendar:
            calendar_lines = [
                f"- {str(event.get('start_time') or event.get('start') or '时间待定')} {str(event.get('summary') or event.get('title') or '未命名安排')[:120]}"
                for event in events[:20]
            ]
            lines.extend(["今日日程：", *(calendar_lines or ["- 暂无日程"]), ""])
        if include_reminders:
            reminder_lines = [f"- {str(item.get('message') or '')[:200]}" for item in reminders[:20]]
            lines.extend(["提醒事项：", *(reminder_lines or ["- 暂无提醒事项"]), ""])
        if research_by_topic is not None:
            lines.append("今天值得看：")
            if research_unavailable:
                lines.append("- 公开信息部分今天暂时没有完成更新。")
            else:
                emitted = 0
                for topic, raw_items in research_by_topic.items():
                    items = list(raw_items)
                    if not items:
                        continue
                    lines.append(f"\n{topic}")
                    for raw in items[:3]:
                        item = raw if isinstance(raw, ResearchEvidenceItem) else ResearchEvidenceItem.from_mapping(raw, topic_label=topic)
                        source = item.publisher or item.canonical_url.split("/", 3)[2]
                        provenance = f"来源：{source}；证据 {item.evidence_id}"
                        if item.published_at:
                            provenance += f"；发布：{item.published_at}"
                        summary, why_relevant = self._summary(item, topic)
                        lines.append(f"- {item.title[:160]}（{provenance}）")
                        lines.append(f"  摘要：{summary}")
                        lines.append(f"  关注点：{why_relevant}")
                        emitted += 1
                if not emitted:
                    lines.append("- 暂无符合条件的公开信息。")
        lines.append("给今天留一点余量，按自己的节奏来就好。")
        result = "\n".join(lines)
        for forbidden in FORBIDDEN_BRIEF_TERMS:
            if forbidden in result:
                raise ValueError("morning brief contains forbidden pressure-model content")
        return result
