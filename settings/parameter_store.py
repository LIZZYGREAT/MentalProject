"""Parameter lookup helpers with explicit aliases and fallbacks."""

from typing import Any, Dict

from settings.model_defaults import PARAM_ALIASES, PARAM_FALLBACKS


def get_param(params: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Return a configured value, supporting aliases and central fallbacks.

    Lookup order:
    1. direct key in ``params``;
    2. compatibility alias, including one-level nested aliases;
    3. project fallback defaults;
    4. caller-supplied ``default``.
    """
    if key in params:
        return params[key]

    alias = PARAM_ALIASES.get(key)
    if isinstance(alias, str) and alias in params:
        return params[alias]
    if isinstance(alias, tuple) and len(alias) == 2:
        parent, child = alias
        parent_cfg = params.get(parent, {})
        if isinstance(parent_cfg, dict) and child in parent_cfg:
            return parent_cfg[child]

    if key in PARAM_FALLBACKS:
        return PARAM_FALLBACKS[key]
    return default

