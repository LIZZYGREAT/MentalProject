from pathlib import Path


MANUAL = Path(__file__).parents[1] / "manuals" / "coding_manual_v0.1.md"


def test_manual_covers_every_schema_variable() -> None:
    text = MANUAL.read_text(encoding="utf-8")
    required = {
        "EVENT_FAMILY",
        "EVENT_SUBTYPE",
        "EVENT_FACTS",
        "LIFECYCLE",
        "OBLIGATION_EXISTS",
        "OBLIGATION_STATUS",
        "PARENT_RELATION",
        "ACTIVE_LEAF",
        "D_POT",
        "U_CONTEXT",
        "D_S",
        "R_POT",
        "M_CONTEXT",
        "MECHANISM EXPOSURE",
        "PARTIAL_ENCODING_BASIS",
        "DEADLINE_SCARCITY_BAND",
        "C_EXEC",
        "IMPORTANCE",
        "C_OUT",
        "U_PERC",
        "F_REC",
        "ORIGIN",
        "ROLE",
        "SUPPORT_GATE",
        "VALIDATION",
        "GUIDANCE",
        "RELEVANCE",
        "PERSONALIZATION",
        "SEEN_STATUS",
    }
    assert not {variable for variable in required if variable not in text}


def test_manual_uses_operational_template_for_constructs() -> None:
    text = MANUAL.read_text(encoding="utf-8")
    assert text.count("**Unit of Annotation:**") >= 29
    assert text.count("**Definition:**") >= 29
    assert text.count("**Question to Annotator:**") >= 29
    assert text.count("**Allowed Labels:**") >= 29
    assert text.count("**Do NOT Infer From:**") >= 29
    assert text.count("**Positive Anchor:**") >= 29
    assert text.count("**Counterexample:**") >= 29
    assert text.count("**Minimal-Pair Example:**") >= 29
