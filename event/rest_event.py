# event/rest_event.py
from datetime import datetime
from typing import Dict, Any, Tuple
import math
import numpy as np  
from event.base import BaseEvent

def _get_deterministic_rng(user, current_time: datetime) -> np.random.RandomState:
    """
    [核心机制：时空哈希种子]
    通过用户的 random_seed 与当前绝对时间（精确到分钟）的组合，
    生成一个局部的、完全可复现的确定性随机数生成器。
    """
    base_seed = int(user.get_param("random_seed", 42))
    time_hash = current_time.year * 10000 + current_time.month * 100 + current_time.day
    time_hash += current_time.hour * 60 + current_time.minute
    
    final_seed = (base_seed + time_hash) % (2**32 - 1)
    return np.random.RandomState(final_seed)


class RestEvent(BaseEvent):
    """日间普通空闲休息碎片 (由 Solver 动态注入或显式声明)"""
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
        """[修复点]：补齐基类抽象方法实现，实现旧接口兼容"""
        ds, _ = self.calculate_stress_impact_dual(user, current_stress, 100.0, current_time, 5)
        return ds
        
    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        idle_dur = self.metadata.get("idle_duration", 0.0)
        S_star = user.get_param("S_star_init", 50)
        
        # 将底层计算完全委托给用户的 RestStrategy
        ds, de = user.rest_strategy.calculate_flow_recovery(current_stress, current_energy, idle_dur, time_step, S_star)
        
        # 更新该休息区块的持续时间
        self.metadata["idle_duration"] += time_step
        return ds, de


class MealEvent(BaseEvent):
    """
    就餐事件 (统一动力学重构版)
    采用：时间加速(1.8x) + 受体饱和软常数保底 + EPOC饱腹余温注入。
    """
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

    # 在 MealEvent 中替换 calculate_stress_impact_dual 方法：
    def calculate_stress_impact_dual(self, user, current_stress: float, current_energy: float, 
                                   current_time: datetime, time_step: int) -> Tuple[float, float]:
        S_star = user.get_param("S_star_init", 50)
        idle_dur = self.metadata.get("idle_duration", 0.0)
        
        # 1. 提取底层动态：免前摇起步 + 温和加速
        # 利用底层的阈值直接跨越慢热期
        inertia_end, cooldown_end = user.rest_strategy.get_phase_thresholds()
        effective_duration = cooldown_end + (idle_dur * 1.5)
        
        base_ds, base_de = user.rest_strategy.calculate_flow_recovery(
            current_stress, current_energy, effective_duration, time_step, S_star
        )

        diff = max(0.0, current_stress - S_star)

        # 2. 自适应软常数保底 
        C_base = 0.04  
        K = 5.0
        guaranteed_drop = -C_base * (diff / (diff + K))

        # 3. 整体事件倍率放大
        meal_multiplier = 1.15 if self.meal_type in ["normal", "early"] else 0.85

        delta_S = (base_ds + guaranteed_drop) * meal_multiplier
        delta_E = base_de * meal_multiplier

        # 4. 注入 EPOC 饱腹感余温
        epoc_injection = 0.5 * (time_step / 5.0)
        user.epoc_level = min(20.0, getattr(user, 'epoc_level', 0.0) + epoc_injection)

        self.metadata["idle_duration"] += time_step
        return delta_S, delta_E


class NapEvent(BaseEvent):
    """
    午睡事件 (统一动力学重构版)
    采用：强效时间加速(2.5x) + 高额软常数兜底 + 睡眠惯性余温，并完美兼容睡眠债偿还机制。
    """
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
        S_star = user.get_param("S_star_init", 50)
        idle_dur = self.metadata.get("idle_duration", 0.0)
        
        # 1. 提取底层动态：免前摇起步 + 较强加速
        inertia_end, cooldown_end = user.rest_strategy.get_phase_thresholds()
        effective_duration = cooldown_end + (idle_dur * 2.0)
        
        base_ds, base_de = user.rest_strategy.calculate_flow_recovery(
            current_stress, current_energy, effective_duration, time_step, S_star
        )

        diff = max(0.0, current_stress - S_star)

        # 2. 自适应软常数保底 
        C_base = 0.08 
        K = 5.0
        guaranteed_drop = -C_base * (diff / (diff + K))

        # 3. 整体事件倍率放大
        nap_multiplier = 1.4 if self.nap_type == "proper" else 1.1

        delta_S = (base_ds + guaranteed_drop) * nap_multiplier
        delta_E = base_de * nap_multiplier
        
        # 4. 睡眠债补偿逻辑保留
        is_repaying = self.metadata.get("is_repaying_debt", False)
        if is_repaying and user.get_sleep_debt() > 0:
            user.reduce_sleep_debt((time_step / 60.0) * 2.0)
            delta_S *= 1.2
            delta_E *= 1.2

        # 5. 注入 EPOC 睡眠惯性余温
        epoc_injection = 1.0 * (time_step / 5.0)
        user.epoc_level = min(30.0, getattr(user, 'epoc_level', 0.0) + epoc_injection)

        self.metadata["idle_duration"] += time_step
        return delta_S, delta_E



class SleepEvent(BaseEvent):
    """[全新实体] 真实的夜间/晨间睡眠事件"""
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