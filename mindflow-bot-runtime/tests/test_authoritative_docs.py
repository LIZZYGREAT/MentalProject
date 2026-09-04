import ast
from datetime import date
import re
from pathlib import Path

from app.agent.tool_registry import ToolRegistry
from app.config import Settings
from app.services.care_effectiveness import CareEffectivenessService
from app.tools.care import CareTools
from mindflow_core.assessment import AssessmentModel
from helpers import memory_database


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = RUNTIME_ROOT.parent


def _marker(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"<!--\s*{name}:\s*([^>]+?)\s*-->", text)
    assert match is not None, f"missing {name} marker in {path}"
    return match.group(1).strip()


def _registry_tools() -> set[str]:
    registry = ToolRegistry()
    CareTools(None, None, None, None, "Asia/Shanghai", None).register(
        registry
    )
    return set(registry.names)


def _documented_tools(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"<!--\s*BUSINESS_TOOLS_BEGIN\s*-->(.*?)"
        r"<!--\s*BUSINESS_TOOLS_END\s*-->",
        text,
        re.DOTALL,
    )
    assert match is not None, f"missing business tool block in {path}"
    names = re.findall(
        r"^\s*-\s+`([a-z0-9_]+)`\s*$", match.group(1), re.MULTILINE
    )
    assert names, f"empty business tool block in {path}"
    assert len(names) == len(set(names)), f"duplicate business tool in {path}"
    assert int(_marker(path, "BUSINESS_TOOL_COUNT")) == len(names)
    return set(names)


def _migration_heads() -> set[str]:
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in (RUNTIME_ROOT / "migrations" / "versions").glob("*.py"):
        values: dict[str, object] = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {
                "revision",
                "down_revision",
            }:
                values[target.id] = ast.literal_eval(node.value)
        revision = values.get("revision")
        assert isinstance(revision, str), f"missing revision in {path}"
        revisions.add(revision)
        down_revision = values.get("down_revision")
        if isinstance(down_revision, str):
            parents.add(down_revision)
        elif isinstance(down_revision, (tuple, list)):
            parents.update(str(item) for item in down_revision)
    return revisions - parents


def test_authoritative_tool_sets_match_registry_exactly():
    expected = _registry_tools()
    for path in (
        RUNTIME_ROOT / "README.md",
        REPOSITORY_ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
    ):
        assert _documented_tools(path) == expected


def test_authoritative_model_version_matches_production_model():
    for path in (
        RUNTIME_ROOT / "README.md",
        REPOSITORY_ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
    ):
        assert _marker(path, "MODEL_VERSION") == AssessmentModel.MODEL_VERSION


def test_authoritative_alembic_head_matches_migration_graph():
    declared_heads = {
        _marker(path, "ALEMBIC_HEAD")
        for path in (
            RUNTIME_ROOT / "README.md",
            REPOSITORY_ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
        )
    }
    assert _migration_heads() == declared_heads


def test_authoritative_card_action_defaults_match_runtime_config():
    fields = Settings.__dataclass_fields__
    transport = fields["feishu_card_action_transport"].default
    callback = fields["feishu_card_callback_enabled"].default
    for path in (
        RUNTIME_ROOT / "README.md",
        REPOSITORY_ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
    ):
        assert _marker(path, "CARD_ACTION_TRANSPORT_DEFAULT") == transport
        assert _marker(path, "CARD_ACTION_CALLBACK_DEFAULT") == str(callback).lower()


def test_authoritative_stage6_effect_boundary_matches_runtime_contract():
    report = CareEffectivenessService(
        memory_database(), "Asia/Shanghai"
    ).descriptive_effects(date(2026, 9, 1), date(2026, 9, 1))
    for path in (
        RUNTIME_ROOT / "README.md",
        REPOSITORY_ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
    ):
        assert _marker(path, "CARE_EFFECT_ANALYSIS_TYPE") == report["analysis_type"]
        assert _marker(path, "CAUSAL_CLAIM_ALLOWED") == str(
            report["causal_claim_allowed"]
        ).lower()
        assert _marker(path, "MRT_RUNTIME_ENABLED") == str(
            report["mrt_runtime_enabled"]
        ).lower()


def test_authoritative_architecture_describes_critical_0018_behaviors():
    text = (REPOSITORY_ROOT / "docs" / "CURRENT_ARCHITECTURE.md").read_text(
        encoding="utf-8"
    )
    assert "point-at-t" in text
    assert "submitted_at` 之前" in text
    assert "重建（因果）" in text
    assert "analysis_kind=reanalysis" in text
    assert "delivery_kind=same_day_late_care" in text
    assert "durable pre-intent saga" in text
    assert "PostgreSQL JSONB" in text
    assert "forecast_currentness_events" in text
    assert "remote_outcome_unknown" in text
    assert "refresh lease" in text
