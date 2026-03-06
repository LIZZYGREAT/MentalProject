# strategy/course_strategy.py
import math
import numpy as np
from typing import Dict, Any, List
from strategy.base import BaseStrategy

class StressFunctionStrategy:
    def __init__(self, params: Dict[str, Any] = None):
        self.params = params or {}
        seed = self.params.get("random_seed", 42)
        self.rng = np.random.RandomState(seed + 100)
        
    def f_s(self, S: float, E: float, S_star: float) -> float:
        raise NotImplementedError

    def get_max_delta(self) -> float:
        # 基类默认后备上限
        return self.params.get("max_delta_base", 3.0)

    def get_energy_drain_modifier(self, E: float) -> float:
        return 1.0

    def _add_noise(self, value: float) -> float:
        scale_noise = self.rng.normal(1.0, 0.125)
        base_noise = self.rng.normal(0, 0.3)
        result = value * scale_noise + base_noise
        return max(0.05, result)


# ==========================================
# 1. 敏感型 (Sensitive) - 下凸加速但限速版
# ==========================================
class SensitiveFunction(StressFunctionStrategy):
    """
    [高敏易破防型 - 快速穿越S型且限速]
    物理意义：前期隐忍（平缓），压差达到 15 时快速进入破防期（下凸加速），
    但最高增速被严格锁死，不会无尽飙升。
    """
    def f_s(self, S: float, E: float, S_star: float) -> float:
        diff = S - S_star
        base = 0.40
        
        if diff <= 0:
            factor = base + 0.005 * abs(diff) 
        else:
            max_extra = 0.70 
            midpoint = 15.0    
            steepness = 0.20   
            
            try:
                exponent = -steepness * (diff - midpoint)
                exponent = max(-50, min(50, exponent))
                factor = base + max_extra / (1.0 + math.exp(exponent))
            except OverflowError:
                factor = base + max_extra
        
        # 精力护盾：满精力略微压制
        shield = 1.10 - 0.35 * (max(0.0, E) / 100.0) 
            
        final_factor = min(factor * shield, 1.5)
        return self._add_noise(final_factor)

    def get_max_delta(self) -> float:
        return self.params.get("max_delta_sensitive", 2.4)

    def get_energy_drain_modifier(self, E: float) -> float:
        return 1.1 if E > 50.0 else 0.9


# ==========================================
# 2. 迟钝型 (Dull)
# ==========================================
class DullFunction(StressFunctionStrategy):
    """
    [迟钝耐受型]
    """
    def f_s(self, S: float, E: float, S_star: float) -> float:
        diff = S - S_star
        threshold = 12.0 
        
        if diff < threshold:
            factor = 0.28
        else:
            factor = 0.28 + 0.012 * (diff - threshold)
            
        shield = 1.0
        safe_zone = 30.0 
        if E > safe_zone:
            ratio = (E - safe_zone) / (100.0 - safe_zone)
            shield = 1.0 - 0.5 * ratio
            
        final_factor = min(factor * shield, 1.5)
        return self._add_noise(final_factor)

    def get_max_delta(self) -> float:
        return self.params.get("max_delta_dull", 2.3)

    def get_energy_drain_modifier(self, E: float) -> float:
        return 0.9 if E > 50.0 else 1.15


# ==========================================
# 3. 饱和型 (Saturated) - 
# ==========================================
class SaturatedFunction(StressFunctionStrategy):
    """
    [标准饱和型 - 上凸减速版]
    物理意义：起步极速上升（1.3倍率），随着压力累计迅速感到“麻木”，
    增幅断崖式下跌，最终维持在较低的常数。
    """
    def f_s(self, S: float, E: float, S_star: float) -> float:
        diff = S - S_star
        floor = 0.30           
        max_capacity = 1.0     
        decay_midpoint = 25.0  
        alpha = 0.15           
        
        if diff <= 0:
            factor = floor + max_capacity
        else:
            try:
                exponent = alpha * (diff - decay_midpoint)
                exponent = max(-50, min(50, exponent))
                factor = floor + max_capacity / (1.0 + math.exp(exponent))
            except OverflowError:
                factor = floor
                
        shield = 1.0
        safe_zone = 40.0
        if E > safe_zone:
            ratio = (E - safe_zone) / (100.0 - safe_zone)
            shield = 1.0 - 0.2 * ratio
            
        return self._add_noise(factor * shield)

    def get_max_delta(self) -> float:
        return self.params.get("max_delta_saturated", 3.0)

    def get_energy_drain_modifier(self, E: float) -> float:
        return 1.05


# ==========================================
# 4. 电池耗竭型 (BatteryDrain)
# ==========================================
class BatteryDrainFunction(StressFunctionStrategy):
    """
    [双变量逻辑斯蒂风险曲面]
    """
    def f_s(self, S: float, E: float, S_star: float) -> float:
        risk = (S - S_star) - (0.5 * E + 5.0)
        try:
            exponent = max(-50, min(50, -0.15 * risk))
            factor = 0.25 + 2.5 / (1.0 + math.exp(exponent)) 
        except OverflowError:
            factor = 0.25
            
        hard_limit = 3.5 if E < 20.0 else 2.0
        return self._add_noise(min(factor, hard_limit))

    def get_max_delta(self) -> float:
        return self.params.get("max_delta_batterydrain", 3.5)


# ========================================================
# C_strategy (连续惩罚)
# ========================================================
class ContinuousPenaltyStrategy:
    def get_threshold(self) -> float: return 3.0
    def get_recovery_rate(self) -> float: return 1.0
    def calculate_fatigue_penalty(self, acc_hours, S_star) -> float: raise NotImplementedError

class HighPenalty(ContinuousPenaltyStrategy):
    def get_threshold(self): return 2.5
    def get_recovery_rate(self): return 1.1
    def calculate_fatigue_penalty(self, acc_hours, S_star):
        threshold = self.get_threshold()
        if acc_hours <= threshold: return 0.0
        over = acc_hours - threshold
        step_ratio = 0.0005 * (over ** 1.5)  
        return S_star * min(step_ratio, 0.0025) 

class ThresholdPenalty(ContinuousPenaltyStrategy):
    def get_threshold(self): return 3.0
    def get_recovery_rate(self): return 1.2
    def calculate_fatigue_penalty(self, acc_hours, S_star):
        threshold = self.get_threshold()
        if acc_hours <= threshold: return 0.0
        over = acc_hours - threshold
        step_ratio = 0.0012 * (1.0 - math.exp(-1.5 * over))  
        return S_star * min(step_ratio, 0.0020) 

class LowPenalty(ContinuousPenaltyStrategy):
    def get_threshold(self): return 3.25
    def get_recovery_rate(self): return 1.6
    def calculate_fatigue_penalty(self, acc_hours, S_star):
        threshold = self.get_threshold()
        if acc_hours <= threshold: return 0.0
        over = acc_hours - threshold
        step_ratio = 0.00025 * over  
        return S_star * min(step_ratio, 0.0016) 


# ==========================================
# CourseStrategy 主类 
# ==========================================
class CourseStrategy(BaseStrategy):
    def __init__(self, f_strategy_type: str, C_strategy_type: str, 
                 time_preferences: List[str], params: Dict[str, Any] = None):
        super().__init__(params)
        self.params = params or {}
        self.f_strategy = self._create_f_strategy(f_strategy_type, self.params)
        self.C_strategy = self._create_C_strategy(C_strategy_type)
        self.time_preferences = time_preferences
        self._setup_time_strategy()
    
    def _create_f_strategy(self, strategy_type: str, params: Dict[str, Any]):
        mapping = {
            "sensitive": SensitiveFunction,
            "dull": DullFunction,
            "saturated": SaturatedFunction,
            "batterydrain": BatteryDrainFunction
        }
        return mapping.get(strategy_type.lower(), SensitiveFunction)(params)
    
    def _create_C_strategy(self, strategy_type: str):
        mapping = {
            "high": HighPenalty,
            "low": LowPenalty,
            "threshold": ThresholdPenalty
        }
        return mapping.get(strategy_type.lower(), HighPenalty)()
    
    def _setup_time_strategy(self):
        combined = {(8,10):1.0, (10,12):1.0, (12,14):1.0,
                   (14,16):1.0, (16,18):1.0, (18,20):1.0, (20,24):1.0}
        
        strategy_map = {
            "like_morning": LikeMorning,
            "dislike_morning": DislikeMorning,
            "like_afternoon": LikeAfternoon,
            "dislike_afternoon": DislikeAfternoon,
            "like_evening": LikeEvening,
            "dislike_evening": DislikeEvening
        }
        
        for pref in self.time_preferences:
            if pref.lower() in strategy_map:
                for (a, b), v in strategy_map[pref.lower()]().weights().items():
                    combined[(a, b)] = v
        
        self.params["time_weights"] = combined
    
    def f_s(self, S: float, E: float, S_star: float = None) -> float:
        S_star = S_star or self.params.get("S_star_init", 50.0)
        return self.f_strategy.f_s(S, E, S_star)
    
    def get_penalty_threshold(self):
        return self.C_strategy.get_threshold()
    
    def get_penalty_recovery_rate(self):
        return self.C_strategy.get_recovery_rate()
    
    def calculate_fatigue(self, continuous_hours: float, S_star: float = None) -> float:
        S_star = S_star or self.params.get("S_star_init", 50.0)
        return self.C_strategy.calculate_fatigue_penalty(continuous_hours, S_star)

    def get_strategy_max_delta(self) -> float:
        return self.f_strategy.get_max_delta()

    def get_energy_drain_modifier(self, E: float) -> float:
        if hasattr(self.f_strategy, 'get_energy_drain_modifier'):
            return self.f_strategy.get_energy_drain_modifier(E)
        return 1.0

    def C_t(self, continuous_hours: float) -> float:
        return 1.0
    
    @staticmethod
    def create(f_type: str, C_type: str, time_prefs: List[str], 
               params: Dict[str, Any] = None) -> 'CourseStrategy':
        return CourseStrategy(f_type, C_type, time_prefs, params)
    
    def get_name(self) -> str:
        return f"CourseStrategy({self.f_strategy.__class__.__name__}, {self.C_strategy.__class__.__name__})"

# 辅助类
class LikeMorning:
    def weights(self): return {(8,10):0.95, (10,12):0.9}

class DislikeMorning:
    def weights(self): return {(8,10):1.1, (10,12):1.05}

class LikeAfternoon:
    def weights(self): return {(12,14):0.95, (14,16):0.95, (16,18):1.0}

class DislikeAfternoon:
    def weights(self): return {(12,14):1.15, (14,16):1.10, (16,18):1.05}

class LikeEvening:
    def weights(self): return {(18,20):0.9, (20,24):0.9}

class DislikeEvening:
    def weights(self): return {(18,20):1.05, (20,24):1.10}

def make_f_strategy(strategy_type: str, params: Dict[str, Any] = None):
    mapping = {
        "sensitive": SensitiveFunction,
        "dull": DullFunction,
        "saturated": SaturatedFunction,
        "batterydrain": BatteryDrainFunction
    }
    return mapping.get(strategy_type.lower(), SensitiveFunction)(params)

def make_C_strategy(strategy_type: str):
    mapping = {
        "high": HighPenalty,
        "low": LowPenalty,
        "threshold": ThresholdPenalty
    }
    return mapping.get(strategy_type.lower(), HighPenalty)()

def setup_time_strategy(strategies, params):
    if not params:
        return
    combined = {(8,10):1.0, (10,12):1.0, (12,14):1.0,
               (14,16):1.0, (16,18):1.0, (18,20):1.0, (20,24):1.0}
    
    for strategy in strategies:
        for (a, b), v in strategy.weights().items():
            combined[(a, b)] = v
    
    params["time_weights"] = combined