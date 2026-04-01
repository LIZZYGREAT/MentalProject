# core_engine/markov_predictor.py
import math
import numpy as np
from typing import Dict, Any, Tuple

class MarkovRegimePredictor:
    """
    [核心引擎] 半马尔可夫情绪区制跳跃预测器 (Semi-Markov Regime-Switching Predictor)
    基于离散时间风险函数 (Discrete-time Hazard Rate) 与 系统承压势能方程 (Phi)。
    消除了魔法常数，实现了特质与环境的严格闭环响应。
    """
    def __init__(self, seed: int = 42, params: Dict[str, Any] = None):
        self.rng = np.random.RandomState(seed + 999)
        self.current_regime = "NORMAL"
        self.regime_duration_minutes = 0.0
        self.params = params or {}
        
    def _calculate_system_potential(self, features: Dict[str, Any], cfg: Dict[str, Any]) -> float:
        """
        计算大一统标量：系统承压势能 (System Stress Potential, Phi)
        范围严格收敛于 [-1.0, 1.0]。
        """
        fatigue = features.get("fatigue", 0.0)
        debt = features.get("debt", 0.0)
        intensity = features.get("intensity", 0.0)
        resilience = features.get("resilience", 0.0)
        event_type = features.get("event_type", "rest")
        
        w_fatigue = cfg.get("w_fatigue", 0.15)
        w_debt = cfg.get("w_debt", 0.30)
        w_intensity = cfg.get("w_intensity", 0.50)
        w_resilience = cfg.get("w_resilience", 0.40)
        
        # 计算场效应护盾
        delta_shield = 0.0
        if event_type == "library":
            delta_shield = cfg.get("shield_library", 0.20)
        elif event_type == "gym":
            delta_shield = cfg.get("shield_gym", 0.50)
        elif event_type in ["rest", "meal", "sleep", "nap"]:
            delta_shield = cfg.get("shield_rest", 0.30)
            
        # 线性融合
        linear_combination = (w_fatigue * fatigue + 
                              w_debt * debt + 
                              w_intensity * intensity - 
                              w_resilience * resilience - 
                              delta_shield)
                              
        # tanh 映射至有界空间
        return math.tanh(linear_combination)

    def predict_next_regime(self, features: Dict[str, Any], elapsed_minutes: float) -> Tuple[str, Dict[str, float]]:
        """
        评估是否发生状态跃迁。
        返回：(新状态, 用于日志的可视化概率指标)
        """
        self.regime_duration_minutes += elapsed_minutes
        cfg = self.params.get("markov_semi_params", {})
        
        # 1. 计算系统承压势能 Phi
        phi = self._calculate_system_potential(features, cfg)
        
        # 2. 将绝对时间转换为宏观驻留步数 (d)
        interval = cfg.get("regime_check_interval", 25)
        d_steps = max(0.1, self.regime_duration_minutes / interval)
        k_shape = cfg.get("k_shape", 1.5)
        
        # 3. 基于当前状态计算动态风险率 (Lambda)
        lam = 0.0
        if self.current_regime == "FLOW":
            lam_base = cfg.get("lambda_base_flow", 0.02)
            gamma = cfg.get("gamma_flow", 2.0)
            lam = lam_base * math.exp(gamma * phi)
            
        elif self.current_regime == "FRICTION":
            lam_base = cfg.get("lambda_base_friction", 0.015)
            gamma = cfg.get("gamma_friction", 1.5)
            lam = lam_base * math.exp(-gamma * phi)
            
        else: # NORMAL
            lam_base = cfg.get("lambda_base_normal", 0.01)
            gamma = cfg.get("gamma_normal", 1.2)
            lam = lam_base * math.cosh(gamma * phi)
            
        # 4. 计算本周期的破裂/跳跃概率 (Weibull Hazard Rate)
        p_jump = 1.0 - math.exp(-lam * (d_steps ** k_shape))
        p_jump = max(0.0, min(0.99, p_jump))
        
        probs_log = {"P_jump": p_jump, "Phi": phi}
        
        # 5. 泊松异常跳跃通道 (处理顿悟或极端崩溃的量子隧穿)
        poisson_prob = cfg.get("poisson_anomaly_prob", 0.01)
        if self.rng.random() < poisson_prob:
            self.regime_duration_minutes = 0.0
            if self.current_regime == "FLOW":
                self.current_regime = "FRICTION"
            elif self.current_regime == "FRICTION":
                self.current_regime = "FLOW"
            else:
                self.current_regime = "FLOW" if phi < 0 else "FRICTION"
            return self.current_regime, probs_log

        # 6. 常规拓扑跳跃判定
        is_jumping = self.rng.random() < p_jump
        
        if is_jumping:
            self.regime_duration_minutes = 0.0
            # 从两极跌落，必须回归 NORMAL
            if self.current_regime in ["FLOW", "FRICTION"]:
                self.current_regime = "NORMAL"
            # 从 NORMAL 分发，依据 Phi 决定方向
            else:
                kappa = cfg.get("kappa_logit", 3.0)
                p_friction = 1.0 / (1.0 + math.exp(-kappa * phi))
                if self.rng.random() < p_friction:
                    self.current_regime = "FRICTION"
                else:
                    self.current_regime = "FLOW"
                    
        return self.current_regime, probs_log

    def apply_regime_modifiers(self, delta_S: float, delta_E: float, features: Dict[str, Any]) -> Tuple[float, float]:
        """
        [动力学注入层] 根据当前区制，向引擎的增量应用动态乘数。
        基于环境变量与特质进行调整，引入严格的矢量方向性，防止恢复反常。
        """
        if self.current_regime == "NORMAL":
            return delta_S, delta_E
            
        mod_cfg = self.params.get("markov_modifiers", {})
        f_strat = features.get("f_strategy", "sensitive")
        intensity = features.get("intensity", 0.0)
        resilience = features.get("resilience", 0.0)
        continuous_hours = features.get("fatigue", 0.0)
        
        if self.current_regime == "FRICTION":
            base_s_mod = mod_cfg.get("friction_s_base", 1.20)
            offset = 0.0
            if f_strat == "sensitive": offset += 0.10
            elif f_strat == "dull": offset -= 0.05
            if intensity > 0.8: offset += 0.05
            
            final_s_mod = base_s_mod + offset
            max_s_mod = mod_cfg.get("friction_s_max", 1.35)
            final_s_mod = max(1.0, min(max_s_mod, final_s_mod))
            final_e_mod = mod_cfg.get("friction_e_base", 1.15)
            
            # 矢量方向性判断：内耗会放大增压/耗能，阻尼减压/恢复
            new_delta_S = delta_S * final_s_mod if delta_S > 0 else delta_S / final_s_mod
            new_delta_E = delta_E * final_e_mod if delta_E < 0 else delta_E / final_e_mod
            
            return new_delta_S, new_delta_E
            
        elif self.current_regime == "FLOW":
            base_s_mod = mod_cfg.get("flow_s_base", 0.75)
            offset = -0.10 * resilience
            if continuous_hours > 2.0:
                offset += 0.05 * (continuous_hours - 2.0)
                
            final_s_mod = base_s_mod + offset
            min_s_mod = mod_cfg.get("flow_s_min", 0.65)
            final_s_mod = max(min_s_mod, min(1.0, final_s_mod))
            final_e_mod = mod_cfg.get("flow_e_base", 0.85)
            
            # 矢量方向性判断：心流会阻尼增压/耗能，放大减压/恢复
            new_delta_S = delta_S * final_s_mod if delta_S > 0 else delta_S / final_s_mod
            new_delta_E = delta_E * final_e_mod if delta_E < 0 else delta_E / final_e_mod
            
            return new_delta_S, new_delta_E

        return delta_S, delta_E