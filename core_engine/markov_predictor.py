# core_engine/markov_predictor.py
import math
import numpy as np
from typing import Dict, Any, Tuple

class MarkovRegimePredictor:
    """半马尔可夫区制（NORMAL/FLOW/FRICTION）：用 Phi 与风险率更新区制，并对 dS、dE 施加方向性乘子。"""
    def __init__(self, seed: int = 42, params: Dict[str, Any] = None):
        """
        参数 seed: 随机数种子；params: 含 markov_semi_params、markov_modifiers 的全局配置。
        """
        self.rng = np.random.RandomState(seed + 999)
        self.current_regime = "NORMAL"
        self.regime_duration_minutes = 0.0
        self.params = params or {}
        self.last_s_mod = 1.0  
        
    def _calculate_system_potential(self, features: Dict[str, Any], cfg: Dict[str, Any]) -> float:
        """承压势能 Phi，融合疲劳、债、强度、韧性与事件护盾，tanh 压到 [-1,1]。"""
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
        根据疲劳、债、强度等算 Phi，再按当前区制算风险率并抽样是否跳变；含少量泊松异常跳变。
        参数:
            features: fatigue/debt/resilience/f_strategy/intensity/event_type；
            elapsed_minutes: 距上次区制检查的分钟数，用于累加驻留时间。
        返回:
            (current_regime, {"P_jump": float, "Phi": float})。
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
        feature_flags = self.params.get("feature_flags", {})
        poisson_enabled = bool(
            feature_flags.get("enable_poisson_anomaly", False)
        ) if isinstance(feature_flags, dict) else False
        poisson_prob = cfg.get("poisson_anomaly_prob", 0.01) if poisson_enabled else 0.0
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

    # 扩展 is_substep 参数，并在非子步截取乘数赋值给 self.last_s_mod
    def apply_regime_modifiers(self, delta_S: float, delta_E: float, features: Dict[str, Any], is_substep: bool = False) -> Tuple[float, float]:
        """
        NORMAL 不变；FRICTION 放大增压与耗能、削弱减压与恢复；FLOW 相反。
        参数 features: f_strategy/intensity/resilience/fatigue（连续小时）。
        返回修正后的 (delta_S, delta_E)。
        """
        if self.current_regime == "NORMAL":
            if not is_substep:
                self.last_s_mod = 1.0
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
            
            if not is_substep:
                self.last_s_mod = final_s_mod
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
            
            if not is_substep:
                self.last_s_mod = final_s_mod
            return new_delta_S, new_delta_E

        if not is_substep:
            self.last_s_mod = 1.0
        return delta_S, delta_E
