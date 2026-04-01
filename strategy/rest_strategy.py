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
        """
        [全面重构] 双轨阻尼-匮乏驱动恢复模型
        S与E彻底解耦。E的恢复采用基于真空吸力与交感抑制的常微分方程(ODE)。
        基底流失(Basal Drain)已移交 Simulator 全局处理，此处仅输出净恢复量。
        """
        K_resilience = self.params.get("K_resilience", 1.0)
        efficiency = self.get_efficiency()
        
        diff = max(0, S - S_star)
        noise_std = self._get_noise_std(diff, E)
        rho = self.params.get("rest_noise_rho", 0.75) 

        # ==========================================
        # 1. 压力动力学 (Stress Dynamics) - 保持原有优良的缓降策略
        # ==========================================
        if diff > 2.0:
            # 远离平衡点：执行具体的子类策略动力学下降
            base_delta_S = self._calculate_dynamics(S, S_star, duration, time_step)
            noise = self._get_ar1_noise(rho, noise_std)
            Z = self.params.get("Z_factor", 0.5)
            Z_mult = 0.8 + 0.4 * Z 
            final_delta_S = base_delta_S * Z_mult + noise
        else:
            # 逼近平衡点：接管为自然游走，弹簧引力 + AR(1)噪声
            pull_coeff = self.params.get("rest_pull_coeff", 0.04)
            final_delta_S = self._simulate_homeostasis_fluctuation(S, S_star, pull_coeff, rho, noise_std * 0.8)

        # 防穿透机制：不允许压力单步跌穿稳态基线过多
        if S + final_delta_S < S_star - 5.0:
            final_delta_S = (S_star - 5.0) - S 

        # ==========================================
        # 2. 认知精力恢复动力学 (Energy Recovery ODE)
        # ==========================================
        ode_cfg = self.params.get("rest_ode_params", {})
        
        # 动态读取超参数，保留默认值作为安全兜底
        R_max = ode_cfg.get("R_max_base", 6.06) * (time_step / 5.0)  
        gamma = ode_cfg.get("deficit_gamma", 2.0)                       
        alpha_inhibit = ode_cfg.get("sympathetic_inhibit_alpha", 0.08)              
        noise_std = ode_cfg.get("energy_noise_std", 0.05)
        
        # A. 真空吸力项 (Deficit Drive)
        deficit_ratio = max(0.0, min(100.0, 100.0 - E)) / 100.0
        vacuum_pull = math.pow(deficit_ratio, gamma)
        
        # B. 交感高压阻尼项 (Stress Inhibition)
        inhibition = math.exp(-alpha_inhibit * diff)
        
        # C. 基础热力学微噪
        e_noise = self.rng.normal(0, noise_std)
        
        # 核心恢复方程
        delta_E = R_max * vacuum_pull * inhibition * efficiency * K_resilience + e_noise
        
        delta_E = max(0.0, delta_E)
        
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
        
        time_decay = 0.15 + 0.85 * math.exp(-duration / 45.0)
        
        base_speed = 0.01 + 0.025 * math.pow(diff, 1.15)
        
        raw_speed = base_speed * time_decay
        
        if diff < 5.0:
            raw_speed *= (0.2 + 0.8 * (diff / 3.0))
            
        max_speed = 1.2
        if raw_speed > max_speed:
            raw_speed = max_speed + 0.3 * math.log1p(raw_speed - max_speed)
            
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
            base_speed = 0.02 + 0.018 * math.pow(diff, 1.1)
        else:
            base_speed = 0.006 + 0.018 * diff
            
        raw_speed = base_speed * time_multiplier
        
        max_speed = 1.2
        if raw_speed > max_speed:
            raw_speed = max_speed + 0.3 * math.log1p(raw_speed - max_speed)
            
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
            base_speed = 0.01 + 0.015 * math.pow(effective_diff, 1.05)
        else:
            base_speed = 0.005 + 0.008 * effective_diff
            
        raw_speed = base_speed * relaxation
        
        if raw_speed > 1.0:
            raw_speed = 1.0 + 0.2 * math.log1p(raw_speed - 1.0)
            
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
            # 倦怠特权：久坐之后开始附带微弱的新陈代谢惩罚
            clamped_dissipation = min(0.01 * math.exp((duration - 60.0) / 120.0), 0.05)
            de -= clamped_dissipation * (time_step / 5.0)
        return ds, de

def make_rest_strategy(strategy_type: str, params: Dict[str, Any] = None):
    if params is None:
        params = {}
    return RestStrategy.create(strategy_type, params)