# strategy/rest_strategy.py
import math
import numpy as np
from typing import Dict, Any, Tuple
from strategy.base import BaseStrategy

class RestStrategy(BaseStrategy):
    """日间空闲恢复：压力子类或稳态游走；精力由 rest_ode_params 匮乏-交感模型给出。"""
    def __init__(self, params: Dict[str, Any] = None):
        super().__init__(params)
        self.params = params or {}
        seed = self.params.get("random_seed", 42)
        self.rng = np.random.RandomState(seed)
        self._last_noise = 0.0 
        self.last_trace = ""

    def _get_ar1_noise(self, rho: float, sigma: float, step_noise: float = 0.0, is_substep: bool = False) -> float:
        """
        一阶自回归噪声：接收步长锁定的 step_noise (N(0,1))，生成连续无尖点的随机波动。
        """
        # 使用外部传入的锁定噪声源替换内部实时采样
        current_noise = rho * self._last_noise + math.sqrt(max(0.0, 1.0 - rho**2)) * sigma * step_noise
        
        # 仅在真实步长结束（非 RK4 子步）时更新动量状态
        if not is_substep:
            self._last_noise = current_noise
            
        return current_noise

    def _simulate_homeostasis_fluctuation(self, current_S: float, target_S: float, 
                                        pull_coeff: float, rho: float, noise_sigma: float, 
                                        step_noise: float = 0.0, is_substep: bool = False) -> float:
        """锚点弹簧项与 AR(1) 噪声叠加。"""
        delta_trend = max(-0.5, min(0.5, pull_coeff * (target_S - current_S)))
        noise = self._get_ar1_noise(rho, noise_sigma, step_noise, is_substep)
        return delta_trend + noise

    def _calculate_dynamics(self, S: float, S_star: float, duration: float, time_step: int) -> float:
        """抽象方法：具体的压力下降动力学曲线。"""
        raise NotImplementedError
    
    def get_efficiency(self) -> float:
        """恢复效率系数。"""
        return 0.50

    def get_phase_thresholds(self) -> Tuple[float, float]:
        """休息相位阈值。"""
        return (5.0, 10.0)

    def get_inertia_energy_rate(self) -> float:
        """惯性耗散率。"""
        return -0.05

    def _get_noise_std(self, diff: float, E: float) -> float:
        """基类默认噪声标准差。"""
        return 0.15
    
    def delta_S_rest(self, S: float, R_t: float, S_star: float, params: Dict[str, Any] = None) -> float:
        """兼容性接口：计算单步压力变化。"""
        time_step = 5 
        if params:
            self.params.update(params)
        base_delta = self._calculate_dynamics(S, S_star, 0.0, time_step)
        Z = self.params.get("Z_factor", 0.5)
        Z_mult = 0.8 + 0.4 * Z
        return base_delta * Z_mult

    def compute_R(self, t_rest: float, params: Dict[str, Any] = None) -> float:
        """计算休息响应系数。"""
        if params:
            self.params.update(params)
        theta = self.params.get("theta", 0.002)
        return 1 - math.exp(-theta * t_rest)
    
    def compute_Z(self) -> float:
        """计算环境因子。"""
        return self.params.get("Z_factor", 0.5)

    def calculate_flow_recovery(self, S: float, E: float, duration: float, 
                              time_step: int, S_star: float, 
                              step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                              is_substep: bool = False) -> Tuple[float, float]:
        """
        日间空闲：压力按子类动力学或稳态游走变化；精力由匮乏驱动 ODE 与交感抑制给出一步 dE。
        接收 step_noise_s (压力) 和 step_noise_e (精力)。
        """
        K_resilience = self.params.get("K_resilience", 1.0)
        efficiency = self.get_efficiency()
        
        diff = max(0, S - S_star)
        noise_std_s = self._get_noise_std(diff, E)
        rho = self.params.get("rest_noise_rho", 0.75) 

        # 1. 压力动力学 (Stress Dynamics)
        if diff > 2.0:
            # 远离平衡点：执行具体的子类策略动力学下降
            base_delta_S = self._calculate_dynamics(S, S_star, duration, time_step)
            # 使用步长锁定的 S 噪声源
            noise_val_s = self._get_ar1_noise(rho, noise_std_s, step_noise_s, is_substep)
            
            Z = self.params.get("Z_factor", 0.5)
            Z_mult = 0.8 + 0.4 * Z 
            final_delta_S = base_delta_S * Z_mult + noise_val_s
            trace_s = f"Base({base_delta_S:.3f}) \\times Z_{{mult}}({Z_mult:.2f}) + Noise({noise_val_s:.3f})"
        else:
            # 接近平衡点：稳态随机游走
            pull_coeff = self.params.get("rest_pull_coeff", 0.04)
            final_delta_S = self._simulate_homeostasis_fluctuation(
                S, S_star, pull_coeff, rho, noise_std_s * 0.8, step_noise_s, is_substep
            )
            trace_s = f"Fluctuation({final_delta_S:.3f})" 

        # 物理防线：防止压力跌穿 S* 过多
        if S + final_delta_S < S_star - 5.0:
            final_delta_S = (S_star - 5.0) - S 
            trace_s = f"ClampToFloor({final_delta_S:.3f})"

        # ==========================================
        # 2. 精力动力学 (Energy Dynamics)
        # ==========================================
        ode_cfg = self.params.get("rest_ode_params", {})
        
        # 基础恢复率按步长缩放
        R_max = ode_cfg.get("R_max_base", 6.06) * (time_step / 5.0)  
        gamma = ode_cfg.get("deficit_gamma", 2.0)                       
        alpha_inhibit = ode_cfg.get("sympathetic_inhibit_alpha", 0.08)              
        noise_std_e = ode_cfg.get("energy_noise_std", 0.05)
        
        # 匮乏度驱动
        deficit_ratio = max(0.0, min(100.0, 100.0 - E)) / 100.0
        vacuum_pull = math.pow(deficit_ratio, gamma)
        
        # 交感神经抑制（高压抑制精力回升）
        inhibition = math.exp(-alpha_inhibit * diff)
        
        # 使用步长锁定的 E 噪声源
        e_noise_val = step_noise_e * noise_std_e
        
        delta_E = R_max * vacuum_pull * inhibition * efficiency * K_resilience + e_noise_val
        delta_E = max(0.0, delta_E)
        
        # 组装底层常微分方程快照
        if not is_substep:
            self.last_trace = (
                f"$$ \\Delta S_{{rest}} = {trace_s} = {final_delta_S:.3f} $$<br>"
                f"$$ \\Delta E_{{rest}} = R_{{max}}({R_max:.2f}) \\times Pull({vacuum_pull:.2f}) \\times "
                f"Inh({inhibition:.2f}) \\times Eff({efficiency:.2f}) \\times K_{{res}}({K_resilience:.2f}) + "
                f"Noise({e_noise_val:.2f}) = {delta_E:.3f} $$"
            )
        
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
    """释然型：高恢复效率，压力随时间迅速衰减。"""
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
        
        time_decay = 0.15 + 0.75 * math.exp(-duration / 30.0)
        
        base_speed = 0.01 + 0.015 * math.pow(diff, 1.05)
        raw_speed = base_speed * time_decay
        
        if diff < 5.0:
            raw_speed *= (0.2 + 0.75 * (diff / 4.0))
            
        max_speed = 1.05
        if raw_speed > max_speed:
            raw_speed = max_speed + 0.3 * math.log1p(raw_speed - max_speed)
            
        return -raw_speed * (time_step / 5.0)


class WarmupRestStrategy(RestStrategy):
    """慢热型：需要较长的休息启动时间，后期恢复斜率极高。"""
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
        
        time_ratio = min(2.0, duration / 20.0)
        time_multiplier = 0.15 + 0.85 * (time_ratio ** 2.25)
        
        if diff > 5.0:
            base_speed = 0.02 + 0.0275 * math.pow(diff, 1.1)
        else:
            base_speed = 0.006 + 0.018 * diff
            
        raw_speed = base_speed * time_multiplier
        
        max_speed = 1.8
        if raw_speed > max_speed:
            raw_speed = max_speed + 0.3 * math.log1p(raw_speed - max_speed)
            
        return -raw_speed * (time_step / 5.0)


class AnxiousRestStrategy(RestStrategy):
    """焦虑型：高压下减压极慢，具有严重的心理“皮筋断裂”效应。"""
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
    """倦怠型：减压速度极慢且精力恢复效率低。"""
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
                               time_step: int, S_star: float, 
                               step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                               is_substep: bool = False) -> Tuple[float, float]:
        """
        [重载]：额外计算倦怠者的耗散惩罚。
        """
        ds, de = super().calculate_flow_recovery(S, E, duration, time_step, S_star, 
                                               step_noise_s, step_noise_e, is_substep)
        
        # 倦怠期惩罚：休息超过 1 小时后，精力开始由于疲软而额外流失
        if duration > 60.0:
            clamped_dissipation = min(0.01 * math.exp((duration - 60.0) / 120.0), 0.05)
            de -= clamped_dissipation * (time_step / 5.0)
            if not is_substep:
                self.last_trace += (
                    f"<br>$$ Burnout_{{penalty}} = -{clamped_dissipation * (time_step / 5.0):.3f} "
                    f"\\Rightarrow \\Delta E = {de:.3f} $$"
                )
        return ds, de

def make_rest_strategy(strategy_type: str, params: Dict[str, Any] = None):
    """工厂方法：根据配置名创建休息策略实例。"""
    if params is None:
        params = {}
    return RestStrategy.create(strategy_type, params)