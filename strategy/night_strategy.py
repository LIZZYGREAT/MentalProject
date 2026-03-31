# strategy/night_strategy.py
import math
import numpy as np
from typing import Dict, Any, Tuple
from datetime import datetime
from strategy.base import BaseStrategy

class NightStrategy(BaseStrategy):
    """
    夜间恢复策略基类 (V5.6 - 强化非线性平衡点引力与动态噪声衰减版)
    包含分阶段动力学、稳态弹簧力，以及用一阶自回归产生的真实、不可导的折线生理电信号。
    """
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.params = params or {}
        
        # 初始化局部随机数生成器，统一使用全局传入的 random_seed
        seed = self.params.get("random_seed", 42)
        self.rng = np.random.RandomState(seed)
        
        # 用于记录上一帧的噪音状态，驱动 AR(1) 过程
        self._last_noise = 0.0

    def calculate_step(self, S: float, E: float, current_time: datetime, time_step: int, elapsed_minutes: float = 0.0) -> Tuple[float, float]:
        raise NotImplementedError

    def _get_asymmetric_amplitude(self, S: float, S_star: float) -> float:
        """非对称动态振幅计算"""
        base_amp = self.params.get("amplitude_base", 0.8)
        k = self.params.get("amplitude_growth_k", 0.04)
        dampen = self.params.get("low_stress_dampen", 0.5)
        
        if S > S_star:
            amp = base_amp * (1.0 + k * (S - S_star))
        else:
            amp = base_amp * dampen
            
        return min(amp, self.params.get("max_amplitude", 3.5))

    def _get_rhythm_factor(self, elapsed_minutes: float, base_period: float = 90.0, phase_shift: float = 0.0) -> float:
        """
        动态拉伸与呼吸扰动长周期节律 (引入随机微小漂移)
        """
        stretched_period = base_period + (elapsed_minutes / 60.0) * 3.0
        random_jitter = self.rng.uniform(-1.5, 1.5)
        period_jitter = stretched_period * 0.05 * math.sin(elapsed_minutes / 200.0)
        final_period = max(40.0, stretched_period + random_jitter + period_jitter)
        
        return math.sin(2 * math.pi * (elapsed_minutes + phase_shift) / final_period)

    def _get_ar1_noise(self, rho: float, sigma: float) -> float:
        """
        一阶自回归 AR(1) 噪音模型
        """
        epsilon = self.rng.normal(0, 1.0)
        current_noise = rho * self._last_noise + math.sqrt(max(0.0, 1.0 - rho**2)) * sigma * epsilon
        self._last_noise = current_noise
        return current_noise

    def _calc_saturated_energy(self, E: float, S: float, time_step: int, multiplier: float = 1.0) -> float:
        """渐进饱和精力恢复模型"""
        base_rec = self.params.get("base_energy_recover", 2.2)
        saturation_factor = max(0.05, (100.0 - E) / 50.0) 
        
        stress_gap = max(0, S - self.params.get("S_star_init", 50.0))
        effective_gap = 40 + (stress_gap - 40) * 0.2 if stress_gap > 40 else stress_gap
            
        return base_rec * saturation_factor * math.exp(-effective_gap / 60.0) * multiplier * (time_step / 5.0)

    @staticmethod
    def create(strategy_type: str, params: Dict[str, Any] = None) -> 'NightStrategy':
        mapping = {
            "normal": NormalNightStrategy,
            "deep": DeepNightStrategy,
            "anxious": AnxiousNightStrategy
        }
        cls = mapping.get(strategy_type.lower(), NormalNightStrategy)
        return cls(params)
    
    def get_name(self) -> str:
        return self.__class__.__name__


class NormalNightStrategy(NightStrategy):
    """
    [标准型]：平稳充电，无硬性压制。
    """
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("night_normal", {})

    def calculate_step(self, S: float, E: float, current_time: datetime, time_step: int, elapsed_minutes: float = 0.0) -> Tuple[float, float]:
        S_star = self.params.get("S_star_init", 50.0)
        diff = S - S_star
        
        rho = self.cfg.get("rho", 0.60)
        sigma = self.cfg.get("sigma", 0.25)
        
        if elapsed_minutes < self.params.get("initial_phase_minutes", 60):
            decay = self.params.get("initial_decay_rate", 0.02) if diff > 0 else 0.005
            delta_S = -decay * diff + 0.3 * self._get_rhythm_factor(elapsed_minutes, 90.0) * (time_step / 5.0) + self._get_ar1_noise(rho, sigma)
            delta_E = self._calc_saturated_energy(E, S, time_step, multiplier=0.9)
            return delta_S, delta_E
        else:
            if diff <= self.params.get("anchor_threshold", 2.5):
                pull_coeff = self.cfg.get("pull_coeff", 0.035) 
                delta_trend = max(-0.5, min(0.5, pull_coeff * (S_star - S)))
            else:
                delta_trend = -self.params.get("oscillation_decay_normal", 0.015) * diff

            delta_rhythm = self._get_asymmetric_amplitude(S, S_star) * self._get_rhythm_factor(elapsed_minutes, 90.0) * (time_step / 5.0)
            
            delta_S = delta_trend + delta_rhythm + self._get_ar1_noise(rho, sigma)
            
            delta_E = self._calc_saturated_energy(E, S, time_step, multiplier=1.0)
            return delta_S, delta_E


class DeepNightStrategy(NightStrategy):
    """
    [深度型]：前段暴击恢复，快速补充能量。
    """
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("night_deep", {})

    def calculate_step(self, S: float, E: float, current_time: datetime, time_step: int, elapsed_minutes: float = 0.0) -> Tuple[float, float]:
        S_star = self.params.get("S_star_init", 50.0)
        diff = S - S_star
        
        rho = self.cfg.get("rho", 0.80)
        sigma = self.cfg.get("sigma", 0.15)
        
        if elapsed_minutes < self.params.get("initial_phase_minutes", 60) - 15:
            delta_S = -self.params.get("initial_decay_rate", 0.02) * 1.3 * diff + self._get_ar1_noise(rho, sigma)
            delta_E = self._calc_saturated_energy(E, S, time_step, multiplier=1.25)
            return delta_S, delta_E
        else:
            if diff <= self.params.get("anchor_threshold", 2.5) + 1.0:
                pull_coeff = self.cfg.get("pull_coeff", 0.05) 
                delta_trend = max(-0.6, min(0.6, pull_coeff * (S_star - S)))
            else:
                delta_trend = -self.params.get("oscillation_decay_deep", 0.02) * diff
            
            rhythm_long = self._get_rhythm_factor(elapsed_minutes, 100.0)
            rhythm_long *= 1.2 if rhythm_long < 0 else 0.4
                
            delta_rhythm = self._get_asymmetric_amplitude(S, S_star) * rhythm_long * (time_step / 5.0)
            delta_S = delta_trend + delta_rhythm + self._get_ar1_noise(rho, sigma)
            
            delta_E = self._calc_saturated_energy(E, S, time_step, multiplier=1.2)
            return delta_S, delta_E


class AnxiousNightStrategy(NightStrategy):
    """
    [焦虑型]：修复波形撕裂，大幅提升充电倍率。
    """
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("night_anxious", {})

    def calculate_step(self, S: float, E: float, current_time: datetime, time_step: int, elapsed_minutes: float = 0.0) -> Tuple[float, float]:
        S_star = self.params.get("S_star_init", 50.0)
        friction = self.params.get("friction_factor", 0.015)
        min_decay = self.params.get("min_decay_rate", 0.006)
        diff = S - S_star
        
        rho = self.cfg.get("rho", 0.55)
        sigma = self.cfg.get("sigma", 0.22) 
        resistance = min(1.0 + np.log1p(np.exp(friction * max(0, diff) * 0.4)), 2.2)
        
        # === 阶段一 ===
        if elapsed_minutes < self.params.get("initial_phase_minutes", 60) + 20: 
            decay = max(self.params.get("initial_decay_rate", 0.02) / resistance, 0.012) 
            delta_S = -decay * diff + self._get_ar1_noise(rho, sigma * 1.3) 
            
            delta_E = self._calc_saturated_energy(E, S, time_step, multiplier=0.85)
            if E + delta_E > 96.0:
                delta_E = max(0.0, 96.0 - E)
            return delta_S, delta_E
            
        # === 阶段二 ===
        else:
            if diff <= self.params.get("anchor_threshold", 2.5):
                pull_coeff = self.cfg.get("pull_coeff", 0.025) 
                delta_trend = max(-0.4, min(0.4, pull_coeff * (S_star - S)))
            else:
                decay = max(self.params.get("oscillation_decay_normal", 0.015) / resistance, min_decay * 1.5)
                delta_trend = -decay * diff
            
            base_amp = self._get_asymmetric_amplitude(S, S_star) * (1.3 if diff <= 0 else 1.0)
            delta_rhythm = base_amp * self._get_rhythm_factor(elapsed_minutes, 75.0, phase_shift=15.0) * (time_step / 5.0)
            
            delta_S = delta_trend + delta_rhythm + self._get_ar1_noise(rho, sigma)
            
            delta_E = self._calc_saturated_energy(E, S, time_step, multiplier=0.95)
            if E + delta_E > 96.0:
                delta_E = max(0.0, 96.0 - E)
            
            return delta_S, delta_E

def make_night_strategy(strategy_type: str, params: Dict[str, Any] = None):
    mapping = {
        "normal": NormalNightStrategy,
        "deep": DeepNightStrategy,
        "anxious": AnxiousNightStrategy
    }
    cls = mapping.get(strategy_type.lower(), NormalNightStrategy)
    return cls(params)