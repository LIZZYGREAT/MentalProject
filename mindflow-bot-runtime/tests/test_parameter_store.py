"""Contract for the central parameter lookup.

``settings.parameter_store.get_param`` keeps a small ``PARAM_FALLBACKS`` net for
sparse caller-supplied ``params``.  The runtime's model parameters live in
``entry.config.GLOBAL_DEFAULT_CONFIG``, so the net must never *decide* an answer
for the current runtime: if it ever disagreed with the global config it would be
an invisible behaviour change.

These tests lock both directions so trimming the fallback map stays safe:

* every declared fallback key exists in ``GLOBAL_DEFAULT_CONFIG`` and holds the
  same value, so removing a fallback cannot change any answer;
* every key the runtime actually asks ``get_param`` for is covered, so the map
  cannot be trimmed below what the runtime reads.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from entry.config import GLOBAL_DEFAULT_CONFIG
from settings.model_defaults import PARAM_FALLBACKS
from settings.parameter_store import get_param

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = PROJECT_ROOT / "mindflow-bot-runtime"

# Modules that call ``User.get_param`` / ``get_param`` on the production path.
CALL_SITE_MODULES = (
    PROJECT_ROOT / "core_engine" / "simulator.py",
    RUNTIME_ROOT / "mindflow_core" / "assessment.py",
)

SKIP_PARTS = {"__pycache__", ".pytest_cache", ".venv", ".git"}
SCAN_DIRS = (
    "algorithm", "calibration", "core_engine", "entity", "entry", "event",
    "services", "settings", "utils", "mindflow-bot-runtime",
)


def _literal_get_param_keys() -> set[str]:
    """Literal string keys passed to ``get_param`` / ``resolve_param``."""

    keys: set[str] = set()
    for base in SCAN_DIRS:
        top = PROJECT_ROOT / base
        if not top.is_dir():
            continue
        for path in top.rglob("*.py"):
            if SKIP_PARTS & set(path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name)
                    else None
                )
                if name not in {"get_param", "resolve_param"}:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.add(first.value)
    return keys


def test_fallback_map_never_overrides_the_global_config() -> None:
    """A declared fallback must agree with the global config exactly."""

    mismatched = {
        key: (value, GLOBAL_DEFAULT_CONFIG.get(key))
        for key, value in PARAM_FALLBACKS.items()
        if key not in GLOBAL_DEFAULT_CONFIG or GLOBAL_DEFAULT_CONFIG[key] != value
    }
    assert mismatched == {}, (
        "PARAM_FALLBACKS disagrees with GLOBAL_DEFAULT_CONFIG; removing the "
        f"fallback would change behaviour: {mismatched}"
    )


def test_runtime_reads_only_covered_keys() -> None:
    """Every key the runtime reads must be present in the fallback net."""

    runtime_keys = set()
    for path in CALL_SITE_MODULES:
        source = path.read_text(encoding="utf-8")
        runtime_keys |= set(
            re.findall(r"""get_param\(\s*["']([^"']+)["']""", source)
        )
    assert runtime_keys, "expected to find get_param call sites"
    # ``ctssm_params`` is read with an explicit ``{}`` default and merged with
    # the global block at the call site, so it must NOT have a fallback.
    uncovered = runtime_keys - set(PARAM_FALLBACKS) - {"ctssm_params"}
    assert uncovered == set(), (
        "the runtime asks get_param for keys with no fallback: "
        f"{sorted(uncovered)}"
    )
    assert "ctssm_params" not in PARAM_FALLBACKS


def test_literal_call_sites_are_covered_or_defaulted() -> None:
    """Non-runtime literal lookups are covered or supply their own default."""

    # ``ctssm_params`` is intentionally absent from the fallback map: callers
    # pass their own default and merge the global coefficient block, so a
    # fallback there could only mask that block.
    allowed_absent = {"ctssm_params"}
    uncovered = _literal_get_param_keys() - set(PARAM_FALLBACKS) - allowed_absent
    assert uncovered == set(), f"unexpected uncovered literal get_param keys: {sorted(uncovered)}"


def test_lookup_order_is_params_then_fallback_then_default() -> None:
    assert get_param({"time_step": 3}, "time_step", 5) == 3
    assert get_param({}, "time_step", 5) == PARAM_FALLBACKS["time_step"]
    assert get_param({}, "not_a_real_key", "sentinel") == "sentinel"
    assert get_param({}, "not_a_real_key") is None


def test_sparse_params_still_resolve_runtime_keys() -> None:
    """A profile that overrides nothing must not yield ``None`` for READ keys."""

    for key in sorted(set(PARAM_FALLBACKS)):
        assert get_param({}, key, None) is not None


def test_fallback_values_match_the_global_config_for_read_keys() -> None:
    for key in ("time_step", "model_family", "E_critical"):
        assert PARAM_FALLBACKS[key] == GLOBAL_DEFAULT_CONFIG[key]
