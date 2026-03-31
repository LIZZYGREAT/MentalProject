# event/rest_event.py
from datetime import datetime
from typing import Dict, Any, Tuple
import math
import numpy as np  
from event.base import BaseEvent

def _get_deterministic_rng(user, current_time: datetime) -> np.random.RandomState:
    base_seed = int(user.get_param("random_seed", 42))
    time_hash = current_time.year * 10000 + current_time.month * 100 + current_time.day
    time_hash += current_time.hour * 60 + current_time.minute
    
    final_seed = (base_seed + time_hash) % (2**32 - 1)
    return np.random.RandomState(final_seed)

class RestEvent(BaseEvent):
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "", description: str = "", 
                 metadata: Dict[str, Any] = None):
        meta = metadata or {}
        if "idle_duration" not in meta:
            meta["idle_duration"] = 0.0
        super().__init__(event_id, start_time, end_time, name, description, meta)
        
    def get_event_type(self) -> str:
        return "rest"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds
        
    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        idle_dur = self.metadata.get("idle_duration", 0.0)
        S_star = user.get_param("S_star_init", 50.0)
        
        ds, de = user.rest_strategy.calculate_flow_recovery(current_stress, current_energy, idle_dur, time_step, S_star)
        
        self.metadata["idle_duration"] += time_step
        return ds, de

class MealEvent(BaseEvent):
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 meal_type: str = "normal", name: str = "就餐", description: str = "", metadata: Dict[str, Any] = None):
        meta = metadata or {}
        meta["meal_type"] = meal_type
        if "idle_duration" not in meta:
            meta["idle_duration"] = 0.0
        super().__init__(event_id, start_time, end_time, name, description, meta)
        self.meal_type = meal_type

    def get_event_type(self) -> str:
        return "meal"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        S_star = user.get_param("S_star_init", 50.0)
        idle_dur = self.metadata.get("idle_duration", 0.0)
        meal_cfg = user.get_param("event_meal", {})
        
        inertia_end, cooldown_end = user.rest_strategy.get_phase_thresholds()
        accel = meal_cfg.get("duration_accel", 1.5)
        effective_duration = cooldown_end + (idle_dur * accel)
        
        base_ds, base_de = user.rest_strategy.calculate_flow_recovery(
            current_stress, current_energy, effective_duration, time_step, S_star
        )

        diff = max(0.0, current_stress - S_star)

        C_base = meal_cfg.get("C_base", 0.04)
        K = meal_cfg.get("K", 5.0)
        guaranteed_drop = -C_base * (diff / (diff + K))

        mult_normal = meal_cfg.get("multiplier_normal", 1.15)
        mult_late = meal_cfg.get("multiplier_late", 0.85)
        meal_multiplier = mult_normal if self.meal_type in ["normal", "early"] else mult_late

        delta_S = (base_ds + guaranteed_drop) * meal_multiplier
        delta_E = base_de * meal_multiplier

        epoc_inj = meal_cfg.get("epoc_injection", 0.5)
        epoc_max = meal_cfg.get("epoc_max", 20.0)
        epoc_step = epoc_inj * (time_step / 5.0)
        user.epoc_level = min(epoc_max, getattr(user, 'epoc_level', 0.0) + epoc_step)

        self.metadata["idle_duration"] += time_step
        return delta_S, delta_E

class NapEvent(BaseEvent):
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 nap_type: str = "proper", name: str = "午睡", description: str = "", metadata: Dict[str, Any] = None):
        meta = metadata or {}
        meta["nap_type"] = nap_type
        if "idle_duration" not in meta:
            meta["idle_duration"] = 0.0
        super().__init__(event_id, start_time, end_time, name, description, meta)
        self.nap_type = nap_type
        
    def get_event_type(self) -> str:
        return "nap"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        S_star = user.get_param("S_star_init", 50.0)
        idle_dur = self.metadata.get("idle_duration", 0.0)
        nap_cfg = user.get_param("event_nap", {})
        
        inertia_end, cooldown_end = user.rest_strategy.get_phase_thresholds()
        accel = nap_cfg.get("duration_accel", 2.0)
        effective_duration = cooldown_end + (idle_dur * accel)
        
        base_ds, base_de = user.rest_strategy.calculate_flow_recovery(
            current_stress, current_energy, effective_duration, time_step, S_star
        )

        diff = max(0.0, current_stress - S_star)

        C_base = nap_cfg.get("C_base", 0.08)
        K = nap_cfg.get("K", 5.0)
        guaranteed_drop = -C_base * (diff / (diff + K))

        mult_proper = nap_cfg.get("multiplier_proper", 1.4)
        mult_short = nap_cfg.get("multiplier_short", 1.1)
        nap_multiplier = mult_proper if self.nap_type == "proper" else mult_short

        delta_S = (base_ds + guaranteed_drop) * nap_multiplier
        delta_E = base_de * nap_multiplier
        
        is_repaying = self.metadata.get("is_repaying_debt", False)
        if is_repaying and user.get_sleep_debt() > 0:
            debt_k = nap_cfg.get("debt_reduce_k", 2.0)
            debt_mult = nap_cfg.get("debt_multiplier", 1.2)
            user.reduce_sleep_debt((time_step / 60.0) * debt_k)
            delta_S *= debt_mult
            delta_E *= debt_mult

        epoc_inj = nap_cfg.get("epoc_injection", 1.0)
        epoc_max = nap_cfg.get("epoc_max", 30.0)
        epoc_step = epoc_inj * (time_step / 5.0)
        user.epoc_level = min(epoc_max, getattr(user, 'epoc_level', 0.0) + epoc_step)

        self.metadata["idle_duration"] += time_step
        return delta_S, delta_E

class SleepEvent(BaseEvent):
    def __init__(self, event_id: str, start_time: str, end_time: str, 
                 name: str = "睡眠", description: str = "", metadata: Dict[str, Any] = None):
        super().__init__(event_id, start_time, end_time, name, description, metadata or {})

    def get_event_type(self) -> str:
        return "sleep"

    def calculate_stress_impact(self, user, current_stress: float, current_time: datetime) -> float:
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds

    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        try:
            if isinstance(self.start_time, str):
                st_h, st_m = map(int, self.start_time[-5:].split(':'))
                st_mins = st_h * 60 + st_m
                ct_mins = current_time.hour * 60 + current_time.minute
                elapsed = ct_mins - st_mins
                if elapsed < 0: elapsed += 24 * 60
            else:
                elapsed = (current_time - self.start_time).total_seconds() / 60.0
        except Exception:
            elapsed = 0.0

        elapsed = max(0.0, elapsed)
        ds, de = user.night_strategy.calculate_step(current_stress, current_energy, current_time, time_step, elapsed)
        return ds, de