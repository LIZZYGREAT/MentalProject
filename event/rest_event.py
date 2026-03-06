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
    """就餐事件 (深度接入策略网并跳过前摇，引入物理保底降压与极低压压缩区)"""
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
        S_star = user.get_param("S_star_init", 50)
        rng = _get_deterministic_rng(user, current_time)
        
        noise_s = rng.normal(0, 0.25)
        noise_e = rng.normal(0, 0.15)

        # 1. 动态获取前摇，跳过惯性与冷却期进入转化期
        _, cooldown_end = user.rest_strategy.get_phase_thresholds()
        idle_dur = self.metadata.get("idle_duration", 0.0)
        effective_duration = idle_dur + cooldown_end + 1.0

        # 2. 从用户的休息策略中获取基础状态流转收益
        base_ds, base_de = user.rest_strategy.calculate_flow_recovery(
            current_stress, current_energy, effective_duration, time_step, S_star
        )

        # === [核心机制：压力差压缩乘数] ===
        diff = max(0.0, current_stress - S_star)
        if diff > 10.0:
            diff_factor = 1.0
        elif diff > 5.0:
            diff_factor = 0.5 + 0.1 * (diff - 5.0)
        else:
            diff_factor = 0.1 + 0.08 * diff

        # 3. 施加双轨制恢复：策略乘数 + 绝对值保底 
        if self.meal_type in ["normal", "early"]:
            # 正常吃饭
            raw_ds = base_ds * 1.18 - 0.35
            delta_S = raw_ds * diff_factor + noise_s
            delta_E = base_de * 1.25 + 0.5 * (time_step / 5.0) + noise_e
        else: 
            # rushed 匆忙扒饭
            raw_ds = base_ds * 1.10 - 0.15
            delta_S = raw_ds * diff_factor + noise_s
            delta_E = base_de * 1.10 + 0.2 * (time_step / 5.0) + noise_e

        self.metadata["idle_duration"] += time_step
        return delta_S, delta_E


class NapEvent(BaseEvent):
    """
    午睡事件 (融入双轨制保底机制、低压压缩区，与 2.0 倍效率的睡眠债联动补偿)
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
        rng = _get_deterministic_rng(user, current_time)
        
        noise_s = rng.normal(0, 0.35)
        noise_e = rng.normal(0, 0.25)

        # 1. 动态获取前摇，直接进入深层恢复
        _, cooldown_end = user.rest_strategy.get_phase_thresholds()
        idle_dur = self.metadata.get("idle_duration", 0.0)
        effective_duration = idle_dur + cooldown_end + 1.0

        # 2. 从策略网中获取基础收益
        base_ds, base_de = user.rest_strategy.calculate_flow_recovery(
            current_stress, current_energy, effective_duration, time_step, S_star
        )

        # 获取睡眠债状态
        is_repaying = self.metadata.get("is_repaying_debt", False)
        sleep_debt = user.get_sleep_debt()

        # 计算已入睡时间 (elapsed_minutes)
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

        # === [核心机制：压力差压缩乘数] ===
        diff = max(0.0, current_stress - S_star)
        if diff > 10.0:
            diff_factor = 1.0
        elif diff > 5.0:
            diff_factor = 0.5 + 0.1 * (diff - 5.0)
        else:
            diff_factor = 0.1 + 0.08 * diff

        # 3. 施加双轨制恢复：策略乘数 + 绝对值保底 (套用压缩乘数)
        if self.nap_type == "proper":
            # 正常午睡
            raw_ds = base_ds * 1.40 - 0.60
            ds = raw_ds * diff_factor
            de = base_de * 1.50

            # 存在睡眠债且系统触发偿还逻辑
            if is_repaying and sleep_debt > 0:
                repay_amount = (time_step / 60.0) * 2.0
                user.reduce_sleep_debt(repay_amount)

                if elapsed <= 40:
                    de *= 1.3
                    ds *= 1.3  # 加速降压
                elif elapsed > 60:
                    de *= 0.8
                    ds = +0.5 * (time_step / 5.0)  

            delta_S = ds + noise_s
            delta_E = de + noise_e

        else:
            # 短促/质量差的午休
            raw_ds = base_ds * 1.25 - 0.30
            delta_S = raw_ds * diff_factor + noise_s
            delta_E = base_de * 1.30 + noise_e

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