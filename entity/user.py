"""Runtime user parameter holder for the production CTSSM."""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core_engine.simulator import Simulator

from algorithm.dynamic_state_model import MODEL_VARIANTS, normalize_model_variant
from entry.config import GLOBAL_DEFAULT_CONFIG
from settings.parameter_store import get_param as resolve_param


class User:
    def __init__(
        self,
        user_id: str = "default",
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.user_id = user_id
        self.params = copy.deepcopy(GLOBAL_DEFAULT_CONFIG)
        if params:
            self.params.update(params)

        selection = self.params.get("model_selection", {})
        if not isinstance(selection, dict):
            selection = {}
        status = str(selection.get("status") or "")
        runtime_authorized = status == "retained_from_empirical_evidence" or (
            status == "stage5_promoted"
            and selection.get("runtime_authorized") is True
        )
        active_variant = (
            normalize_model_variant(selection.get("active_variant", "m0"))
            if runtime_authorized
            else "m0"
        )
        self.params["model_family"] = MODEL_VARIANTS[active_variant]["canonical"]
        self.params.setdefault("model_selection", {})["active_variant"] = active_variant
        self.current_sleep_debt = 0.0

        from core_engine.simulator import Simulator

        self.solver: Simulator = Simulator(self)

    def set_sleep_debt(self, debt_hours: float) -> None:
        self.current_sleep_debt = max(0.0, float(debt_hours))

    def get_sleep_debt(self) -> float:
        return self.current_sleep_debt

    def get_current_S_star(self) -> float:
        return float(self.params.get("S_star_init", 50.0))

    def get_current_threshold(self) -> float:
        alert_config = self.params.get("alert_thresholds", {})
        if isinstance(alert_config, dict) and "yellow_stress" in alert_config:
            return max(
                float(alert_config["yellow_stress"]),
                self.get_current_S_star() + 12.0,
            )
        return float(self.params.get("S_threshold", 90.0))

    def get_param(self, key: str, default: Any = None) -> Any:
        return resolve_param(self.params, key, default)
