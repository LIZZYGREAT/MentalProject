# strategy/course_strategy.py
import math
import numpy as np
from typing import Dict, Any, List
from strategy.base import BaseStrategy

class StressFunctionStrategy:
    def __init__(self, params: Dict[str, Any] = None):
        """
        承载 params 配置与基础随机种子。
        """
        self.params = params or {}
        seed = self.params.get("random_seed", 42)
        self.rng = np.random.RandomState(seed + 100)
        self.last_trace = ""
        
    def f_s(self, S: float, E: float, S_star: float, step_noise_s: float = 0.0) -> float:
        """
        接收外部传入的 step_noise_s (N(0,1) 采样)，确保步长内一致。
        """
        raise NotImplementedError

    def get_max_delta(self) -> float:
        """
        获取当前策略允许的最大变化幅度。
        """
        return self.params.get("max_delta_base", 3.0)

    def get_allostatic_stress_amplifier(self, E: float) -> float:
        """
        非稳态压力放大器：精力越低，压力生成率越高。
        调低低精力时的恐慌性惩罚上限与突变陡峭度。
        """
        E_r = max(0.0, min(100.0, E)) / 100.0
        E_c = self.params.get("allostatic_collapse_point", 0.35)
        k = self.params.get("allostatic_collapse_steepness", 7.5) 
        lambda_penalty = self.params.get("allostatic_max_penalty", 0.15) 
        
        try:
            exponent = max(-50, min(50, -k * (E_r - E_c)))
            amplifier = 1.0 + lambda_penalty * (1.0 - 1.0 / (1.0 + math.exp(exponent)))
        except OverflowError:
            amplifier = 1.0 + lambda_penalty
            
        return amplifier

    def get_energy_drain_modifier(self, E: float) -> float:
        """
        非稳态耗精倍率，精力越低通常越大。
        维持 alpha 约束，统一调谐 beta 使得消耗曲线过渡更自然。
        """
        E_r = max(0.0, min(100.0, E)) / 100.0
        alpha = self.params.get("allostatic_cost_alpha", 0.75)
        beta = self.params.get("allostatic_cost_beta", 2.0)
        return 1.0 + alpha * math.exp(-beta * E_r)

    def _apply_locked_noise(self, value: float, step_noise_s: float) -> float:
        """
        将随机性逻辑从内部采样改为接收外部锁定源。
        保持了原有的噪声强度系数：scale (0.125) 与 bias (0.3)。
        """
        result = value * (1.0 + step_noise_s * 0.05) 
        return max(0.05, result)


class SensitiveFunction(StressFunctionStrategy):
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("f_strategy_params", {}).get("sensitive", {})

    def f_s(self, S: float, E: float, S_star: float, step_noise_s: float = 0.0) -> float:
        diff = S - S_star
        base = self.cfg.get("base", 0.40)
        
        if diff <= 0:
            factor = base + 0.005 * abs(diff)
            trace_str = f"Base({base:.2f}) + 0.005 \\times |{diff:.1f}|"
        else:
            max_extra = self.cfg.get("max_extra", 0.70)
            midpoint = self.cfg.get("midpoint", 15.0)
            steepness = self.cfg.get("steepness", 0.18)
            try:
                exponent = -steepness * (diff - midpoint)
                exponent = max(-50, min(50, exponent))
                factor = base + max_extra / (1.0 + math.exp(exponent))
            except OverflowError:
                factor = base + max_extra
            trace_str = f"{base:.2f} + \\frac{{{max_extra:.2f}}}{{1 + e^{{{-steepness:.2f}({diff:.1f} - {midpoint:.1f})}}}}"
                
        amp = self.get_allostatic_stress_amplifier(E)
        final_factor = factor * amp
        noisy_factor = self._apply_locked_noise(final_factor, step_noise_s)
        
        self.last_trace = (
            f"$$ f_s(Sens) = \\left({trace_str}\\right) \\times Amp({amp:.2f}) = {final_factor:.2f} "
            f"\\xrightarrow{{step\\_noise}} {noisy_factor:.2f} $$"
        )
        return noisy_factor

    def get_max_delta(self) -> float:
        return self.params.get("max_delta_sensitive", 2.4)


class DullFunction(StressFunctionStrategy):
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("f_strategy_params", {}).get("dull", {})

    def f_s(self, S: float, E: float, S_star: float, step_noise_s: float = 0.0) -> float:
        diff = S - S_star
        threshold = self.cfg.get("threshold", 12.0)
        base = self.cfg.get("base", 0.50)
        k = self.cfg.get("k", 0.012)
        
        if diff < threshold:
            factor = base
            trace_str = f"Base({base:.2f})"
        else:
            factor = base + k * pow(diff - threshold, 1.1)
            trace_str = f"Base({base:.2f}) + {k:.3f}({diff:.1f} - {threshold:.1f})"
            
        amp = self.get_allostatic_stress_amplifier(E)
        final_factor = factor * amp
        noisy_factor = self._apply_locked_noise(final_factor, step_noise_s)
        
        self.last_trace = (
            f"$$ f_s(Dull) = \\left({trace_str}\\right) \\times Amp({amp:.2f}) = {final_factor:.2f} "
            f"\\xrightarrow{{step\\_noise}} {noisy_factor:.2f} $$"
        )
        return noisy_factor

    def get_max_delta(self) -> float:
        return self.params.get("max_delta_dull", 2.3)


class SaturatedFunction(StressFunctionStrategy):
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("f_strategy_params", {}).get("saturated", {})

    def f_s(self, S: float, E: float, S_star: float, step_noise_s: float = 0.0) -> float:
        diff = S - S_star
        floor = self.cfg.get("floor", 0.30)
        max_capacity = self.cfg.get("max_capacity", 1.0)
        decay_midpoint = self.cfg.get("decay_midpoint", 15.0)
        alpha = self.cfg.get("alpha", 0.15)
        
        if diff <= 0:
            factor = floor + max_capacity
            trace_str = f"{floor:.2f} + {max_capacity:.2f}"
        else:
            try:
                exponent = alpha * (diff - decay_midpoint)
                exponent = max(-50, min(50, exponent))
                factor = floor + max_capacity / (1.0 + math.exp(exponent))
            except OverflowError:
                factor = floor
            trace_str = f"{floor:.2f} + \\frac{{{max_capacity:.2f}}}{{1 + e^{{{alpha:.2f}({diff:.1f} - {decay_midpoint:.1f})}}}}"
                
        amp = self.get_allostatic_stress_amplifier(E)
        final_factor = factor * amp
        noisy_factor = self._apply_locked_noise(final_factor, step_noise_s)
        
        self.last_trace = (
            f"$$ f_s(Sat) = \\left({trace_str}\\right) \\times Amp({amp:.2f}) = {final_factor:.2f} "
            f"\\xrightarrow{{step\\_noise}} {noisy_factor:.2f} $$"
        )
        return noisy_factor

    def get_max_delta(self) -> float:
        return self.params.get("max_delta_saturated", 3.0)


class BatteryDrainFunction(StressFunctionStrategy):
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("f_strategy_params", {}).get("batterydrain", {})

    def f_s(self, S: float, E: float, S_star: float, step_noise_s: float = 0.0) -> float:
        e_k = self.cfg.get("e_k", 0.4)
        e_b = self.cfg.get("e_b", 5.0)
        steepness = self.cfg.get("steepness", 0.3)
        base = self.cfg.get("base", 0.45)
        max_extra = self.cfg.get("max_extra", 0.85)
        
        risk = (S - S_star) - (e_k * E + e_b)
        try:
            exponent = max(-50, min(50, -steepness * risk))
            factor = base + max_extra / (1.0 + math.exp(exponent)) 
        except OverflowError:
            factor = base
            
        trace_str = f"{base:.2f} + \\frac{{{max_extra:.2f}}}{{1 + e^{{{-steepness:.2f} \\times Risk({risk:.1f})}}}}"
        
        amp = self.get_allostatic_stress_amplifier(E)
        final_factor = factor * amp
        noisy_factor = self._apply_locked_noise(final_factor, step_noise_s)
        
        self.last_trace = (
            f"$$ f_s(Batt) = \\left({trace_str}\\right) \\times Amp({amp:.2f}) = {final_factor:.2f} "
            f"\\xrightarrow{{step\\_noise}} {noisy_factor:.2f} $$"
        )
        return noisy_factor

    def get_max_delta(self) -> float:
        return self.params.get("max_delta_batterydrain", 3.5)


# ========================================================
# C_strategy (连续负荷惩罚)
# ========================================================

class ContinuousPenaltyStrategy:
    def __init__(self, params: Dict[str, Any] = None):
        self.params = params or {}
        self.cfg_group = self.params.get("c_strategy_params", {})
        self.last_penalty_trace = ""
        
    def get_threshold(self) -> float: raise NotImplementedError
    def get_recovery_rate(self) -> float: raise NotImplementedError
    def calculate_fatigue_penalty(self, acc_hours, S_star) -> float: raise NotImplementedError

class HighPenalty(ContinuousPenaltyStrategy):
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.cfg_group.get("high", {"threshold": 2.5, "rec_rate": 1.1, "k": 0.005, "max_penalty": 0.025, "exp": 1.5})

    def get_threshold(self): return self.cfg.get("threshold", 2.5)
    def get_recovery_rate(self): return self.cfg.get("rec_rate", 1.1)
    
    def calculate_fatigue_penalty(self, acc_hours, S_star):
        threshold = self.get_threshold()
        if acc_hours <= threshold: 
            self.last_penalty_trace = ""
            return 0.0
        over = acc_hours - threshold
        k = self.cfg.get("k", 0.005)
        exp_val = self.cfg.get("exp", 1.5)
        max_p = self.cfg.get("max_penalty", 0.025)
        step_ratio = k * (over ** exp_val)
        penalty = S_star * min(step_ratio, max_p)
        self.last_penalty_trace = f"$$ f_{{pen}}(High) = S^*({S_star:.1f}) \\times \\min({max_p}, {k:.3f} \\times ({over:.1f})^{{{exp_val:.1f}}}) = {penalty:.3f} $$"
        return penalty

class ThresholdPenalty(ContinuousPenaltyStrategy):
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.cfg_group.get("threshold", {"threshold": 3.0, "rec_rate": 1.2, "k": 0.012, "max_penalty": 0.020, "exp_k": -1.5})

    def get_threshold(self): return self.cfg.get("threshold", 3.0)
    def get_recovery_rate(self): return self.cfg.get("rec_rate", 1.2)
    
    def calculate_fatigue_penalty(self, acc_hours, S_star):
        threshold = self.get_threshold()
        if acc_hours <= threshold: 
            self.last_penalty_trace = ""
            return 0.0
        over = acc_hours - threshold
        k = self.cfg.get("k", 0.012)
        exp_k = self.cfg.get("exp_k", -1.5)
        max_p = self.cfg.get("max_penalty", 0.020)
        step_ratio = k * (1.0 - math.exp(exp_k * over))
        penalty = S_star * min(step_ratio, max_p)
        self.last_penalty_trace = f"$$ f_{{pen}}(Thresh) = S^*({S_star:.1f}) \\times \\min({max_p}, {k:.3f}(1 - e^{{{exp_k:.1f} \\times {over:.1f}}})) = {penalty:.3f} $$"
        return penalty

class LowPenalty(ContinuousPenaltyStrategy):
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.cfg_group.get("low", {"threshold": 3.25, "rec_rate": 1.6, "k": 0.0025, "max_penalty": 0.016})

    def get_threshold(self): return self.cfg.get("threshold", 3.25)
    def get_recovery_rate(self): return self.cfg.get("rec_rate", 1.6)
    
    def calculate_fatigue_penalty(self, acc_hours, S_star):
        threshold = self.get_threshold()
        if acc_hours <= threshold: 
            self.last_penalty_trace = ""
            return 0.0
        over = acc_hours - threshold
        k = self.cfg.get("k", 0.0025)
        max_p = self.cfg.get("max_penalty", 0.016)
        step_ratio = k * over
        penalty = S_star * min(step_ratio, max_p)
        self.last_penalty_trace = f"$$ f_{{pen}}(Low) = S^*({S_star:.1f}) \\times \\min({max_p}, {k:.4f} \\times {over:.1f}) = {penalty:.3f} $$"
        return penalty


# ==========================================
# CourseStrategy 主类
# ==========================================

class CourseStrategy(BaseStrategy):
    def __init__(self, f_strategy_type: str, C_strategy_type: str, 
                 time_preferences: List[str], params: Dict[str, Any] = None):
        super().__init__(params)
        self.params = params or {}
        self.f_strategy = self._create_f_strategy(f_strategy_type, self.params)
        self.C_strategy = self._create_C_strategy(C_strategy_type, self.params)
        self.time_preferences = time_preferences
        self.last_penalty_trace = ""
        self.time_weights = {}
        self._setup_time_strategy()
    
    def _create_f_strategy(self, strategy_type: str, params: Dict[str, Any]):
        mapping = {
            "sensitive": SensitiveFunction,
            "dull": DullFunction,
            "saturated": SaturatedFunction,
            "batterydrain": BatteryDrainFunction
        }
        return mapping.get(strategy_type.lower(), SensitiveFunction)(params)
    
    def _create_C_strategy(self, strategy_type: str, params: Dict[str, Any]):
        mapping = {
            "high": HighPenalty,
            "low": LowPenalty,
            "threshold": ThresholdPenalty
        }
        return mapping.get(strategy_type.lower(), HighPenalty)(params)
    
    def _setup_time_strategy(self):
        combined = dict(self.params.get("time_weights", {
            (8, 10): 1.0, (10, 12): 1.0, (12, 14): 1.0,
            (14, 16): 1.0, (16, 18): 1.0, (18, 20): 1.0, (20, 24): 1.0,
        }))
        pref_cfg = self.params.get("time_pref_weights", {})
        for pref in self.time_preferences:
            pref_key = pref.lower()
            if pref_key in pref_cfg:
                for k_str, v in pref_cfg[pref_key].items():
                    if isinstance(k_str, str) and ',' in k_str:
                        clean_str = k_str.strip('() ')
                        a, b = map(int, clean_str.split(','))
                        combined[(a, b)] = v
                    elif isinstance(k_str, tuple):
                        combined[k_str] = v
        self.time_weights = combined

    def get_time_weight(self, hour: int) -> float:
        """Return the preference-adjusted time weight for an event start hour."""
        for (start_h, end_h), weight in self.time_weights.items():
            if start_h <= hour < end_h:
                return weight
        return 1.0
    
    def f_s(self, S: float, E: float, S_star: float = None, step_noise_s: float = 0.0) -> float:
        """
        透传步长锁定噪声到具体的敏感度函数。
        """
        S_star = S_star or self.params.get("S_star_init", 50.0)
        return self.f_strategy.f_s(S, E, S_star, step_noise_s)
    
    def get_penalty_threshold(self): 
        return self.C_strategy.get_threshold()
        
    def get_penalty_recovery_rate(self): 
        return self.C_strategy.get_recovery_rate()
        
    def calculate_fatigue(self, continuous_hours: float, S_star: float = None) -> float:
        S_star = S_star or self.params.get("S_star_init", 50.0)
        pen = self.C_strategy.calculate_fatigue_penalty(continuous_hours, S_star)
        self.last_penalty_trace = getattr(self.C_strategy, "last_penalty_trace", "")
        return pen

    def get_strategy_max_delta(self) -> float: 
        return self.f_strategy.get_max_delta()
        
    def get_energy_drain_modifier(self, E: float) -> float:
        """非稳态耗精倍率。"""
        if hasattr(self.f_strategy, 'get_energy_drain_modifier'):
            return self.f_strategy.get_energy_drain_modifier(E)
        return 1.0

    def C_t(self, continuous_hours: float) -> float: 
        return 1.0
    
    @staticmethod
    def create(f_type: str, C_type: str, time_prefs: List[str], 
               params: Dict[str, Any] = None) -> 'CourseStrategy':
        """工厂方法。"""
        return CourseStrategy(f_type, C_type, time_prefs, params)
    
    def get_name(self) -> str:
        return f"CourseStrategy({self.f_strategy.__class__.__name__}, {self.C_strategy.__class__.__name__})"

def make_f_strategy(strategy_type: str, params: Dict[str, Any] = None):
    mapping = {
        "sensitive": SensitiveFunction,
        "dull": DullFunction,
        "saturated": SaturatedFunction,
        "batterydrain": BatteryDrainFunction
    }
    return mapping.get(strategy_type.lower(), SensitiveFunction)(params)

def make_C_strategy(strategy_type: str, params: Dict[str, Any] = None):
    mapping = {
        "high": HighPenalty,
        "low": LowPenalty,
        "threshold": ThresholdPenalty
    }
    return mapping.get(strategy_type.lower(), HighPenalty)(params)

def setup_time_strategy(strategies, params): 
    pass
