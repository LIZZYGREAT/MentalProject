# strategy/rest_strategy.py
import math
import numpy as np
from typing import Dict, Any, Tuple
from strategy.base import BaseStrategy

class RestStrategy(BaseStrategy):
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.params = params or {}
        seed = self.params.get("random_seed", 42)
        self.rng = np.random.RandomState(seed)
        self._last_noise = 0.0 

    def _get_ar1_noise(self, rho: float, sigma: float) -> float:
        """一阶自回归噪声：生成连续无尖点的随机波动"""
        epsilon = self.rng.normal(0, 1.0)
        current_noise = rho * self._last_noise + math.sqrt(max(0.0, 1.0 - rho**2)) * sigma * epsilon
        self._last_noise = current_noise
        return current_noise

    def _simulate_homeostasis_fluctuation(self, current_S: float, target_S: float, pull_coeff: float, rho: float, noise_sigma: float) -> float:
        """
        [大一统机制] 统一稳态波动函数：弹簧引力 + AR(1)游走
        接收目标锚点与引力系数，模拟生物体在平衡态下的自然波澜。
        """
        delta_trend = max(-0.5, min(0.5, pull_coeff * (target_S - current_S)))
        noise = self._get_ar1_noise(rho, noise_sigma)
        return delta_trend + noise

    def _calculate_dynamics(self, S: float, S_star: float, duration: float, time_step: int) -> float:
        raise NotImplementedError
    
    def get_efficiency(self) -> float:
        return 0.50

    def get_phase_thresholds(self) -> Tuple[float, float]:
        return (5.0, 10.0)

    def get_inertia_energy_rate(self) -> float:
        return -0.05

    def _get_alpha(self, duration: float) -> float:
        if duration <= 40.0:
            return 1.0
        elif duration <= 120.0:
            return math.exp(-(duration - 40.0) / 40.0)
        else:
            return 0.05

    def _get_noise_std(self, diff: float, E: float) -> float:
        """基类默认噪声标准差"""
        return 0.15
    
    def delta_S_rest(self, S: float, R_t: float, S_star: float, params: Dict[str, Any] = None) -> float:
        time_step = 5 
        if params:
            self.params.update(params)
        base_delta = self._calculate_dynamics(S, S_star, 0.0, time_step)
        Z = self.params.get("Z_factor", 0.5)
        Z_mult = 0.8 + 0.4 * Z
        return base_delta * Z_mult

    def compute_R(self, t_rest: float, params: Dict[str, Any] = None) -> float:
        if params:
            self.params.update(params)
        theta = self.params.get("theta", 0.002)
        return 1 - math.exp(-theta * t_rest)
    
    def compute_Z(self) -> float:
        return self.params.get("Z_factor", 0.5)

    def calculate_flow_recovery(self, S: float, E: float, duration: float, 
                              time_step: int, S_star: float) -> Tuple[float, float]:
        K_resilience = self.params.get("K_resilience", 1.0)
        efficiency = self.get_efficiency()
        
        diff = max(0, S - S_star)
        
        noise_std = self._get_noise_std(diff, E)
        rho = self.params.get("rest_noise_rho", 0.75) 
        
        inertia_end, cooldown_end = self.get_phase_thresholds()

        if duration <= inertia_end:
            noise = self._get_ar1_noise(rho, noise_std)
            delta_S = 0.0 + noise * 0.5
            delta_E = self.get_inertia_energy_rate() * (time_step / 5.0)
            return delta_S, delta_E

        if duration <= cooldown_end:
            noise = self._get_ar1_noise(rho, noise_std)
            delta_S = -0.04 * (time_step / 5.0) + noise * 0.8
            delta_E = 0.0
            return delta_S, delta_E

        # 核心恢复期
        if E < 30.0:
            e_bonus = 0.90  
        elif E <= 70.0:
            e_bonus = 1.00  
        else:
            e_bonus = 1.0 + ((E - 70.0) / 10.0) * 0.05
            e_bonus = min(e_bonus, 1.15)
        
        Z = self.params.get("Z_factor", 0.5)
        Z_mult = 0.8 + 0.4 * Z 

        if diff > 2.0:
            # 远离平衡点：执行具体的策略动力学下降
            base_delta_S = self._calculate_dynamics(S, S_star, duration, time_step)
            noise = self._get_ar1_noise(rho, noise_std)
            final_delta_S = base_delta_S * Z_mult * e_bonus + noise
        else:
            # [统一稳态波动机制] 逼近平衡点：接管为自然游走，弹簧引力 + AR(1)噪声
            pull_coeff = self.params.get("rest_pull_coeff", 0.04) # 日间引力适中
            final_delta_S = self._simulate_homeostasis_fluctuation(S, S_star, pull_coeff, rho, noise_std * 0.8)

        # 防穿透
        if S + final_delta_S < S_star - 5.0:
            final_delta_S = (S_star - 5.0) - S 

        # --- 以下精力计算部分保持不变 ---
        if E < 20.0 or efficiency <= 0.0:
            conversion_rate = 0.0
        else:
            conversion_rate = 0.75 * K_resilience * efficiency * self._get_alpha(duration)
            
        stress_drop = abs(min(0, final_delta_S)) if diff > 0 else 0.0
        delta_E_conversion = stress_drop * conversion_rate
        
        delta_E_metabolism = 1.25 * efficiency * (time_step / 5.0) * self._get_alpha(duration)
        delta_E = delta_E_conversion + delta_E_metabolism

        if duration > 120.0:
            delta_E -= 0.015 * (time_step / 5.0)
            
        return final_delta_S, delta_E

    @staticmethod
    def create(strategy_type: str, params: Dict[str, Any] = None) -> 'RestStrategy':
        mapping = {
            "relieved": RelievedRestStrategy,
            "warmup": WarmupRestStrategy,
            "anxious": AnxiousRestStrategy,
            "burnout": BurnoutRestStrategy
        }
        cls = mapping.get(strategy_type.lower(), RelievedRestStrategy)
        return cls(params)
    
    def get_name(self) -> str:
        return self.__class__.__name__


class RelievedRestStrategy(RestStrategy):
    """
    [释然型] - 前期快，随后基于时间与压差双重平滑衰减，全波段下凸着陆 (彻底移除生硬阈值)
    """
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("rest_relieved", {})
        
    def get_phase_thresholds(self) -> Tuple[float, float]: 
        res = self.cfg.get("phase_thresholds", [2.0, 5.0])
        return (float(res[0]), float(res[1]))

    def get_efficiency(self) -> float: 
        return self.cfg.get("efficiency", 1.05)
    
    def _get_noise_std(self, diff: float, E: float) -> float: 
        return self.cfg.get("noise_std", 0.12)
        
    def get_inertia_energy_rate(self) -> float: 
        return self.cfg.get("inertia_e_rate", -0.05)

    def _calculate_dynamics(self, S: float, S_star: float, duration: float, time_step: int) -> float:
        diff = max(0, S - S_star)
        
        time_decay = 0.4 + 0.6 * math.exp(-duration / 30.0)
        
        base_speed = 0.015 + 0.035 * math.pow(diff, 0.8)
        
        raw_speed = base_speed * time_decay
        
        if diff < 5.0:
            raw_speed *= (0.2 + 0.8 * (diff / 3.0))
            
        max_speed = 0.6
        if raw_speed > max_speed:
            raw_speed = max_speed + 0.2 * math.log1p(raw_speed - max_speed)
            
        return -raw_speed * (time_step / 5.0)


class WarmupRestStrategy(RestStrategy):
    """
    [慢热型] - 时间与压差的彻底解耦 (75分钟超长前摇，四次方极致压制解锁机制)
    """
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("rest_warmup", {})
        
    def get_phase_thresholds(self) -> Tuple[float, float]: 
        res = self.cfg.get("phase_thresholds", [5.0, 15.0])
        return (float(res[0]), float(res[1]))
    
    def get_efficiency(self) -> float: 
        return self.cfg.get("efficiency", 1.0)

    def _get_noise_std(self, diff: float, E: float) -> float: 
        return self.cfg.get("noise_std", 0.10)
        
    def get_inertia_energy_rate(self) -> float: 
        return self.cfg.get("inertia_e_rate", -0.05)

    def _calculate_dynamics(self, S: float, S_star: float, duration: float, time_step: int) -> float:
        diff = max(0, S - S_star)
        
        time_ratio = min(1.0, duration / 60.0)
        time_multiplier = 0.15 + 0.85 * (time_ratio ** 3.5)
        
        if diff > 5.0:
            base_speed = 0.028 + 0.015 * diff
        else:
            base_speed = 0.006 + 0.018 * diff
            
        raw_speed = base_speed * time_multiplier
        
        max_speed = 0.8
        if raw_speed > max_speed:
            raw_speed = max_speed + 0.2 * math.log1p(raw_speed - max_speed)
            
        return -raw_speed * (time_step / 5.0)


class AnxiousRestStrategy(RestStrategy):
    """
    [焦虑型] - 高压死锁，中段皮筋断裂，低压死水残留 (基础速率再次下调50%)
    """
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("rest_anxious", {})
        
    def get_phase_thresholds(self) -> Tuple[float, float]: 
        res = self.cfg.get("phase_thresholds", [10.0, 15.0])
        return (float(res[0]), float(res[1]))
    
    def get_inertia_energy_rate(self) -> float: 
        return self.cfg.get("inertia_e_rate", -0.06)
    
    def get_efficiency(self) -> float: 
        return self.cfg.get("efficiency", 0.85)

    def _get_noise_std(self, diff: float, E: float) -> float: 
        return self.cfg.get("noise_std", 0.15) 

    def _calculate_dynamics(self, S: float, S_star: float, duration: float, time_step: int) -> float:
        diff = max(0, S - S_star)
        
        exponent = max(-50, min(50, -0.15 * (duration - 25.0)))
        relaxation = 0.2 + (0.8 / (1.0 + math.exp(exponent)))
        
        effective_diff = max(0.0, diff - 5.0)

        if effective_diff > 5.0:
            base_speed = 0.012 + 0.006 * effective_diff
        else:
            base_speed = 0.005 + 0.005 * effective_diff
            
        raw_speed = base_speed * relaxation
        return -raw_speed * (time_step / 5.0)


class BurnoutRestStrategy(RestStrategy):
    """
    [倦怠型] - 极度平坦的对数死水，全波段缓降 (基础速率再次下调50%)
    """
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.cfg = self.params.get("rest_burnout", {})
        
    def get_phase_thresholds(self) -> Tuple[float, float]: 
        res = self.cfg.get("phase_thresholds", [5.0, 10.0])
        return (float(res[0]), float(res[1]))
    
    def get_efficiency(self) -> float: 
        return self.cfg.get("efficiency", 0.70)

    def _get_noise_std(self, diff: float, E: float) -> float: 
        return self.cfg.get("noise_std", 0.04) 
        
    def get_inertia_energy_rate(self) -> float: 
        return self.cfg.get("inertia_e_rate", -0.05)

    def _calculate_dynamics(self, S: float, S_star: float, duration: float, time_step: int) -> float:
        diff = max(0, S - S_star)
        
        if diff > 5.0:
            raw_speed = 0.0035 + 0.020 * math.log1p(diff)
        else:
            raw_speed = 0.00125 + 0.005 * diff
            
        return -raw_speed * (time_step / 5.0)

    def calculate_flow_recovery(self, S: float, E: float, duration: float,
                               time_step: int, S_star: float) -> Tuple[float, float]:
        ds, de = super().calculate_flow_recovery(S, E, duration, time_step, S_star)
        if duration > 60.0:
            clamped_dissipation = min(0.01 * math.exp((duration - 60.0) / 120.0), 0.05)
            de -= clamped_dissipation * (time_step / 5.0)
        return ds, de

def make_rest_strategy(strategy_type: str, params: Dict[str, Any] = None):
    if params is None:
        params = {}
    return RestStrategy.create(strategy_type, params)