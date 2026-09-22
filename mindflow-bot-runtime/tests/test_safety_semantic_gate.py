import pytest

from app.services.safety_service import (
    FIXED_HIGH_RISK_RESPONSE,
    SafetySemanticGate,
    SafetyService,
)


@pytest.mark.parametrize(
    "text",
    (
        "帮我总结一篇关于自杀预防的论文",
        "新闻里提到了自杀率，这个数据是什么意思？",
        "小说里引用了‘想死’这句台词，请分析人物",
    ),
)
def test_academic_self_harm_discussion_not_immediate_self_risk(text):
    gate = SafetySemanticGate()
    assert gate.classify(text) == "quoted_or_academic_context"
    assert SafetyService(gate).precheck(text) is None


def test_third_party_concern_not_misclassified_as_self_imminent():
    text = "朋友说过想死，我应该怎么帮助他？"
    gate = SafetySemanticGate()
    assert gate.classify(text) == "third_party_concern"
    assert SafetyService(gate).precheck(text) is None


@pytest.mark.parametrize(
    "text", ("我现在想自杀", "我准备结束生命", "我不想活了")
)
def test_explicit_first_person_imminent_risk_still_fixed_response(text):
    service = SafetyService()
    assert service.semantic_gate.classify(text) == "self_imminent_risk"
    assert service.precheck(text) == FIXED_HIGH_RISK_RESPONSE


def test_classifier_failure_uses_conservative_backend_fallback():
    class BrokenGate:
        def classify(self, _text):
            raise RuntimeError("classifier unavailable")

    service = SafetyService(BrokenGate())
    assert service.precheck("有人提到想死") == FIXED_HIGH_RISK_RESPONSE
    assert service.precheck("普通的学习问题") is None
