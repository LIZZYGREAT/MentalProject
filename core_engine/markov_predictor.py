# core_engine/markov_predictor.py
import math
import numpy as np
from typing import Dict, Any, Tuple

class MarkovRegimePredictor:
    """
    [核心引擎] 事件驱动的情绪区制跳跃预测器 (Event-Driven Markov Chain)
    引入环境场效应(Intensity/Event Type)、底层毒药、性格护盾，以及带温度与截断的柔性 Softmax 博弈。
    """
    def __init__(self, seed: int = 42):
        self.rng = np.random.RandomState(seed + 999)
        self.current_regime = "NORMAL"
        self.regime_duration_steps = 0
        # 高温平滑：数值越大，三者概率越容易均分；数值越小，越容易赢者通吃
        self.temperature = 1.5  

    def predict_next_regime(self, features: Dict[str, Any]) -> Tuple[str, Dict[str, float]]:
        # 1. 提取生态与系统特征
        fatigue = features.get("fatigue", 0.0)
        debt = features.get("debt", 0.0)
        resilience = features.get("resilience", 0.0)
        
        state = features.get("current_state", "DAY_ACTIVE")
        has_high_load = features.get("has_high_load", False)
        
        f_strat = features.get("f_strategy", "sensitive")
        c_strat = features.get("c_strategy", "high")
        

        intensity = features.get("intensity", 0.0)
        event_type = features.get("event_type", "rest")

        # === 绝对静默区 ===
        # 夜间深度休息时，情绪机制强制挂起，不再做无意义的内耗与心流判定
        if state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
            self.current_regime = "NORMAL"
            self.regime_duration_steps += 1
            return "NORMAL", {"NORMAL": 1.0, "FLOW": 0.0, "FRICTION": 0.0}

        # === 战局 1：内耗推力 (Z_friction) ===
        # 底层毒药：疲劳与睡眠债的线性压迫
        base_fric = fatigue * 0.4 + debt * 0.8
        if c_strat == "high":
            base_fric *= 1.2
            
        # 环境压迫：难度带来的额外推力
        intensity_push = 0.0
        if intensity > 0.8:
            intensity_push = (intensity - 0.85) * 4.0  # T5(0.85)基本无推力, T1(1.1)带来1.0的强推力
            if f_strat == "sensitive":
                intensity_push *= 1.5  # 高敏体质在难题面前内耗翻倍
                
        # 场效应与护盾抵扣
        if event_type == "library":
            intensity_push *= 0.6  # 自主环境极大削弱压迫感
        elif event_type == "gym":
            base_fric -= 1.5       # 运动强效阻断焦虑
        elif not has_high_load:
            base_fric -= 1.0       # 处于普通休息态时，内耗衰退
            
        resilience_shield = max(0.0, resilience) * 0.8
        Z_friction = base_fric + intensity_push - resilience_shield

        # === 战局 2：心流吸力 (Z_flow) ===
        # 一票否决：熬夜、极度疲劳或欠睡时，心流直接斩断
        if debt > 1.2 or fatigue > 4.0 or state == "LATE_NIGHT_ACTIVE":
            Z_flow = -2.0
        else:
            # 挑战与技能(韧性)的乘性共鸣
            challenge_match = (intensity - 0.75) * (resilience + 0.5) * 2.5 if intensity > 0 else -1.0
            
            # 环境场效应附加分
            autonomy_bonus = 0.0
            if event_type == "library":
                autonomy_bonus = 0.8   # 图书馆掌控感加成
            elif event_type == "gym":
                autonomy_bonus = 1.2   # 运动内啡肽极易产生心流
            elif not has_high_load:
                autonomy_bonus = -3.0  # 无所事事时不可能有心流
                
            Z_flow = challenge_match + autonomy_bonus

        # === 战局 3：稳态锚点 (Z_normal) ===
        Z_normal = 1.5
        if not has_high_load or event_type in ["rest", "meal"]:
            Z_normal = 3.5  # 回归休息时，极大概率被拉回正常态

        # 惯性加成 (物理学定律：倾向于保持当前状态)
        if self.current_regime == "NORMAL": Z_normal += 0.5
        elif self.current_regime == "FLOW": Z_flow += 0.5
        elif self.current_regime == "FRICTION": Z_friction += 0.5

        # === 终极防线：数值截断 (防止概率坍缩为 0 或 100%) ===
        Z_normal = max(-1.5, min(3.5, Z_normal))
        Z_flow = max(-1.5, min(2.5, Z_flow))
        Z_friction = max(-1.5, min(2.5, Z_friction))

        # === 带温度的 Softmax 概率映射 ===
        logits = {
            "NORMAL": Z_normal / self.temperature,
            "FLOW": Z_flow / self.temperature,
            "FRICTION": Z_friction / self.temperature
        }
        
        max_logit = max(logits.values()) 
        exp_probs = {k: math.exp(v - max_logit) for k, v in logits.items()}
        sum_exp = sum(exp_probs.values())
        probs = {k: v / sum_exp for k, v in exp_probs.items()}
        
        # 轮盘赌跳跃
        regimes = ["NORMAL", "FLOW", "FRICTION"]
        p_array = [probs["NORMAL"], probs["FLOW"], probs["FRICTION"]]
        next_regime = self.rng.choice(regimes, p=p_array)
        
        # 维护驻留时长
        if next_regime == self.current_regime:
            self.regime_duration_steps += 1
        else:
            self.regime_duration_steps = 0
            
        self.current_regime = next_regime
        return next_regime, probs

    def apply_regime_modifiers(self, delta_S: float, delta_E: float) -> Tuple[float, float]:
        if self.current_regime == "FLOW":
            return delta_S * 0.4, delta_E * 0.6
        elif self.current_regime == "FRICTION":
            return delta_S * 1.5, delta_E * 1.3
        return delta_S, delta_E