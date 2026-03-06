# core_engine/markov_predictor.py
import math
import numpy as np
from typing import Dict, Any, Tuple

class MarkovRegimePredictor:
    """
    [核心引擎] 15分钟情绪区制跳跃预测器 (Context-Aware Markov Chain)
    引入了非线性疲劳重塑 (Non-linear Fatigue Shaping) 与心流预热红利。
    """
    def __init__(self, seed: int = 42):
        self.rng = np.random.RandomState(seed + 999)
        self.current_regime = "NORMAL"
        self.regime_duration_steps = 0
        self.temperature = 1.3  

    def predict_next_regime(self, features: Dict[str, Any]) -> Tuple[str, Dict[str, float]]:
        # 1. 提取生态与系统特征
        fatigue = features.get("fatigue", 0.0)
        debt = features.get("debt", 0.0)
        stress_gap = features.get("stress_gap", 0.0)
        resilience = features.get("resilience", 0.0)
        
        state = features.get("current_state", "DAY_ACTIVE")
        has_high_load = features.get("has_high_load", False)
        
        f_strat = features.get("f_strategy", "sensitive")
        c_strat = features.get("c_strategy", "high")
        r_strat = features.get("rest_strategy", "relieved")
        n_strat = features.get("night_strategy", "normal")

        # 2. 基础 Logits 锚点
        Z_normal = 0.0
        Z_flow = -0.5 + 1.2 * resilience
        Z_friction = -2.0 - 1.5 * resilience

        # === 3. 核心重构：非线性疲劳重塑 (Non-linear Fatigue Shaping) ===
        capped_debt = min(debt, 3.0)

        if has_high_load or state in ["LATE_NIGHT_ACTIVE", "NIGHT_OVERTIME"]:
            # A. 策略护盾：计算“有效疲劳”
            if c_strat == "low":
                eff_fatigue = max(0.0, fatigue - 0.75) 
            elif c_strat == "threshold":
                eff_fatigue = max(0.0, fatigue - 0.7) * 1.25 
            else: # high
                eff_fatigue = max(0.0,fatigue-0.65)
                
            # B. 下凸惩罚曲面
            fric_penalty = (eff_fatigue ** 2.0) / 6.0
            flow_penalty = (eff_fatigue ** 1.5) / 5.0
            
            # C. 心流预热红利
            flow_warmup = 0.0
            if 0.5 < fatigue < 1.5:
                flow_warmup = 0.8 * max(0.0, 1.0 - abs(fatigue - 1.25) / 1.0)

            # D. 应用重塑后的参数
            Z_flow += flow_warmup - flow_penalty - 0.3 * capped_debt
            Z_friction += fric_penalty + 1.2 * max(0.0, stress_gap - 0.3)
            
            # E. 敏感度策略微调
            if f_strat == "sensitive":
                Z_friction += 0.8 + 0.3 * eff_fatigue 
                Z_flow -= 0.5
            elif f_strat == "dull":
                Z_friction -= 1.2  
                Z_flow += 0.2 
            elif f_strat == "batterydrain":
                Z_friction += 1.0 * max(0.0, stress_gap) 
                
        elif state in ["ROUTINE_MAINTENANCE", "DAY_ACTIVE"]:
            # [休息态]
            if r_strat == "relieved":
                Z_flow += 1.5 + 0.8 * max(0.0, 1.0 - fatigue / 5.0) 
                Z_friction -= 2.0
            elif r_strat == "anxious":
                Z_flow -= 1.0
                Z_friction += 1.0 + 0.5 * stress_gap 
            elif r_strat == "warmup":
                if self.current_regime == "FLOW":
                    Z_flow += 1.2
                else:
                    Z_flow += 0.2
        
        elif state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
            # [睡眠态]
            if n_strat == "anxious":
                Z_friction += 1.0 
            elif n_strat == "deep":
                Z_flow += 1.5

        # 4. 绝地反击与均值回归 (Breakthrough & Mean Reversion)
        # A. 状态粘性护城河：给予当前所处状态极大的留存权重，消除 15 分钟高频震荡
        stickiness_bonus = 1.5
        if self.current_regime == "FLOW":
            Z_flow += stickiness_bonus
        elif self.current_regime == "FRICTION":
            Z_friction += stickiness_bonus
        elif self.current_regime == "NORMAL":
            Z_normal += stickiness_bonus

        # B. 均值回归弹簧：拉长忍耐期。
        if self.current_regime == "FLOW" and self.regime_duration_steps >= 3:
            Z_flow -= 0.8 * (self.regime_duration_steps / 4.0) 
        elif self.current_regime == "FRICTION" and self.regime_duration_steps >= 3:
            Z_friction -= 1.0 * (self.regime_duration_steps / 4.0) 
            Z_flow += 1.0 * (self.regime_duration_steps / 4.0)

        # 5. 终极数值防爆限幅 (Logit Clamping)
        Z_flow = max(-2.5, min(2.5, Z_flow))
        Z_friction = max(-2.5, min(2.5, Z_friction))

        # 6. 带温度的 Softmax 映射
        logits = {
            "NORMAL": Z_normal / self.temperature,
            "FLOW": Z_flow / self.temperature,
            "FRICTION": Z_friction / self.temperature
        }
        
        max_logit = max(logits.values()) 
        exp_probs = {k: math.exp(v - max_logit) for k, v in logits.items()}
        sum_exp = sum(exp_probs.values())
        probs = {k: v / sum_exp for k, v in exp_probs.items()}
        
        # 7. 轮盘赌跳跃
        regimes = ["NORMAL", "FLOW", "FRICTION"]
        p_array = [probs["NORMAL"], probs["FLOW"], probs["FRICTION"]]
        next_regime = self.rng.choice(regimes, p=p_array)
        
        # 8. 维护驻留时长
        if next_regime == self.current_regime:
            self.regime_duration_steps += 1
        else:
            self.regime_duration_steps = 0
            
        self.current_regime = next_regime
        
        return next_regime, probs

    def apply_regime_modifiers(self, base_ds: float, base_de: float) -> Tuple[float, float]:
        """输出乘数控制 (微调：让心流的奖励更明显，内耗的惩罚不至于瞬间爆表)"""
        if self.current_regime == "FRICTION":
            return base_ds * 1.15, base_de * 1.2  
        elif self.current_regime == "FLOW":
            return base_ds * 0.65, base_de * 0.75  # 增强心流的保护力
        return base_ds, base_de