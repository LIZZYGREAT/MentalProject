import ast
import re
from pathlib import Path

from app.agent.tool_registry import ToolRegistry
from app.tools.care import CareTools
from mindflow_core.assessment import AssessmentModel


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = RUNTIME_ROOT.parent


def _marker(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"<!--\s*{name}:\s*([^>]+?)\s*-->", text)
    assert match is not None, f"missing {name} marker in {path}"
    return match.group(1).strip()


def _tool_count() -> int:
    registry = ToolRegistry()
    CareTools(None, None, None, None, None, "Asia/Shanghai", None).register(
        registry
    )
    return len(registry.names)


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


def test_authoritative_tool_count_matches_registry():
    expected = str(_tool_count())
    assert _marker(RUNTIME_ROOT / "README.md", "BUSINESS_TOOL_COUNT") == expected
    assert (
        _marker(
            REPOSITORY_ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
            "BUSINESS_TOOL_COUNT",
        )
        == expected
    )


def test_authoritative_model_version_matches_production_model():
    assert _marker(
        REPOSITORY_ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
        "MODEL_VERSION",
    ) == AssessmentModel.MODEL_VERSION


def test_authoritative_alembic_head_matches_migration_graph():
    declared = _marker(
        REPOSITORY_ROOT / "docs" / "CURRENT_ARCHITECTURE.md",
        "ALEMBIC_HEAD",
    )
    assert _migration_heads() == {declared}
