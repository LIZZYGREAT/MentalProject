"""Constrained language composition for already-authorized care interventions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Mapping, Protocol

import httpx

from app.contracts.care_evidence import CareEvidencePacket


CARE_COMPOSER_PROMPT_VERSION = "care_composer.zh.v1"


class CareComposerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CareDraft:
    message: str
    selected_fact_ids: tuple[str, ...]
    selected_reason_codes: tuple[str, ...]
    intervention_type: str
    action_minutes: int
    model: str | None = None
    prompt_version: str = CARE_COMPOSER_PROMPT_VERSION

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, model: str | None = None
    ) -> "CareDraft":
        return cls(
            message=str(payload.get("message") or ""),
            selected_fact_ids=tuple(
                str(item) for item in list(payload.get("selected_fact_ids") or [])[:4]
            ),
            selected_reason_codes=tuple(
                str(item) for item in list(payload.get("selected_reason_codes") or [])[:3]
            ),
            intervention_type=str(payload.get("intervention_type") or ""),
            action_minutes=int(payload.get("action_minutes") or 0),
            model=str(payload.get("model") or model or "") or None,
            prompt_version=str(payload.get("prompt_version") or CARE_COMPOSER_PROMPT_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["selected_fact_ids"] = list(self.selected_fact_ids)
        value["selected_reason_codes"] = list(self.selected_reason_codes)
        return value


class CareComposerProtocol(Protocol):
    async def compose(self, evidence: CareEvidencePacket) -> CareDraft: ...


CARE_COMPOSER_SYSTEM_PROMPT = """你是 MindFlow 的关怀文案组织器。只输出 JSON 对象，不输出推理过程。
只能使用输入 CareEvidencePacket 中的事实；forecast 是模型预测，不能写成用户已经真实感受到。
最多选择 1–2 个主要 reason code，先说明为什么此时提醒，再给一个具体、低压力、可选的建议。
不诊断、不贴心理标签、不虚构成绩/DDL/睡眠/情绪，不改变 Backend 给出的 intervention_type 或 action_minutes。
事实不足时坦诚简化，不强行个性化。message 使用自然简洁的中文，120–360 字。
返回字段：message、selected_fact_ids、selected_reason_codes、intervention_type、action_minutes。"""


class OpenAICompatibleCareComposerClient:
    """Minimal JSON client; participant consent is checked by the scheduler."""

    def __init__(self, url: str, api_key: str, model: str, *, timeout_seconds: float = 8.0):
        self.url = str(url).strip()
        self.api_key = str(api_key).strip()
        self.model = str(model).strip()
        self.timeout_seconds = max(1.0, min(30.0, float(timeout_seconds)))

    async def compose(self, evidence: CareEvidencePacket) -> CareDraft:
        if not self.url or not self.api_key or not self.model:
            raise CareComposerUnavailable("care composer is not configured")
        body = {
            "model": self.model,
            "temperature": 0.35,
            "stream": False,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": CARE_COMPOSER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(evidence.to_dict(), ensure_ascii=False),
                },
            ],
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        response.raise_for_status()
        payload = response.json()
        content = payload.get("output_text") if isinstance(payload, Mapping) else None
        if not content and isinstance(payload, Mapping):
            choices = payload.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, list):
            content = "".join(str(item.get("text") or "") for item in content if isinstance(item, Mapping))
        if not isinstance(content, str) or not content.strip():
            raise CareComposerUnavailable("care composer returned no JSON content")
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise CareComposerUnavailable("care composer returned malformed JSON") from exc
        if not isinstance(decoded, Mapping):
            raise CareComposerUnavailable("care composer result must be an object")
        return CareDraft.from_payload(decoded, model=self.model)


class CareAgentComposer:
    """Adapt a testable provider into the stable CareComposerProtocol."""

    def __init__(self, provider: Any, *, model: str | None = None):
        self.provider = provider
        self.model = model or getattr(provider, "model", None)
        self.timeout_seconds = float(getattr(provider, "timeout_seconds", 8.0))

    async def compose(self, evidence: CareEvidencePacket) -> CareDraft:
        if self.provider is None:
            raise CareComposerUnavailable("care composer is unavailable")
        result = await self.provider.compose(evidence)
        if isinstance(result, CareDraft):
            return result
        if isinstance(result, Mapping):
            return CareDraft.from_payload(result, model=self.model)
        raise CareComposerUnavailable("care composer result has an unsupported shape")
