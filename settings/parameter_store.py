"""Parameter lookup helpers with central fallbacks."""

from typing import Any, Dict

from settings.model_defaults import PARAM_FALLBACKS


def get_param(params: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Return a configured value with a central fallback.

    Lookup order:
    1. direct key in ``params``;
    2. project fallback defaults (see ``PARAM_FALLBACKS``);
    3. caller-supplied ``default``.

    The runtime reads only ``time_step``, ``model_family``, ``ctssm_params``
    and ``E_critical``.  All four also live in
    ``entry.config.GLOBAL_DEFAULT_CONFIG``, so step 2 currently never decides
    an answer; it stays as a defensive net for sparse caller-supplied
    ``params`` and is locked by ``tests/test_parameter_store.py``.
    """
    if key in params:
        return params[key]

    if key in PARAM_FALLBACKS:
        return PARAM_FALLBACKS[key]
    return default
