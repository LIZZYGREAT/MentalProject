from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.contracts import ResearchEvidenceItem, ResearchJobSpec, evidence_envelope
from app.policy import ExecPolicy, is_public_ip


def test_job_rejects_private_context_and_bounds_fields():
    with pytest.raises(ValueError, match="private"):
        ResearchJobSpec.from_mapping({"topic": "AI", "participant_id": "secret"})
    with pytest.raises(ValueError):
        ResearchJobSpec.from_mapping({"topic": "AI", "query_hints": ["x"] * 9})


def test_job_is_public_context_only_and_serializable():
    job = ResearchJobSpec.from_mapping({"topic": "游戏行业", "source_kinds": ["web", "github"]})
    assert job.as_dict()["topic"] == "游戏行业"
    assert job.as_dict()["source_kinds"] == ["web", "github"]


def test_ip_policy_rejects_internal_addresses():
    assert is_public_ip("8.8.8.8")
    assert not is_public_ip("127.0.0.1")
    assert not is_public_ip("10.0.0.2")
    assert not is_public_ip("169.254.169.254")


def test_exec_policy_is_argv_only_and_allowlisted(tmp_path):
    policy = ExecPolicy(tmp_path)
    assert policy.validate(("python3", "-c", "print('ok')"), timeout_seconds=2)
    with pytest.raises(ValueError):
        policy.validate(("bash", "-lc", "echo bad"), timeout_seconds=2)
    with pytest.raises(ValueError):
        policy.validate(("python3", "-c", "print(1); print(2)"), timeout_seconds=2)


def test_evidence_is_untrusted_and_provenance_is_stable():
    item = ResearchEvidenceItem.build(
        source_kind="web", title="Title", canonical_url="https://example.com/a#fragment",
        content="public evidence", extraction_mode="http", freshness_hours=24,
    )
    envelope = evidence_envelope([item])
    assert item.canonical_url == "https://example.com/a"
    assert item.evidence_id
    assert "untrusted_public_evidence_only" in envelope
    assert "public evidence" in envelope

