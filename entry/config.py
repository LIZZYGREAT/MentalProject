# config.py
"""
全局默认配置中心 (Memory-based Configuration)
为云端 Agent 部署优化，消除所有本地 JSON 强依赖。
包含了系统所有底层算法常数、策略基线与默认参数。
"""

GLOBAL_DEFAULT_CONFIG = {
    # ==========================================
    # 1. 基础权重参数 (CIS 课程强度评估)
    # ==========================================
    "w1": 0.4, 
    "w2": 0.2, 
    "w3": 0.3,
    "lambda_like": 0.25,
    "time_weights": {
        (8, 10): 1.1, (10, 12): 0.9, (12, 14): 1.2,
        (14, 16): 1.0, (16, 18): 0.9, (18, 20): 1.05,
        (20, 24): 1.1
    },
    
    # ==========================================
    # 2. 心理状态与环境基数 (Z因子)
    # ==========================================
    "Z_awake": 0.5, 
    "Z_avoid": 0.0, 
    "Z_cogload": 0.2,
    "Z_info": 0.2, 
    "Z_help": 0.1, 
    "Z_valence": 0.0,
    "Z_factor": 0.5,
    "K_resilience": 1.0,  # 心理韧性全局乘数
    
    # ==========================================
    # 3. 核心物理常数 & 仿真控制
    # ==========================================
    "S_star_init": 50.0,  # 初始压力平衡点
    "S_threshold": 90.0,  # 报警阈值
    "E_critical": 20.0,   # 精力耗竭线
    "time_step": 5,       # 仿真步长
    "random_seed": 42,
    "noise_scale_factor": 0.2,
    "max_delta_base": 3.0,
    
    # ==========================================
    # 4. 课程与疲劳消耗参数 
    # ==========================================
    "alpha_cis_drain": 1.0,
    "cognitive_weight": 0.65,
    "fatigue_acceleration": 1.20, # 即将弃用，由非稳态阻尼接管
    
    # ==========================================
    # 5. 用户默认策略偏好
    # ==========================================
    "f_strategy": "sensitive", 
    "C_strategy": "high",        
    "night_strategy": "normal",  
    "rest_strategy": "relieved",
    "time_preferences": [],
    
    # ==========================================
    # 6. 非稳态负荷理论参数 (Allostatic Load Dynamics) 
    # ==========================================
    "allostatic_collapse_point": 0.3,      # E_r 崩塌临界点 (30%精力)
    "allostatic_collapse_steepness": 10.0, # Sigmoid 崩塌陡峭度
    "allostatic_max_penalty": 1.5,         # 彻底破防时的最大乘性放大器
    "allostatic_cost_alpha": 2.0,          # 边际耗能指数衰减底数
    "allostatic_cost_beta": 4.0,           # 边际耗能指数衰减速率
    
    # ==========================================
    # 7. 全局系统性惩罚参数 (Global Penalties) 
    # ==========================================
    "penalty_circadian": {
        "drain_multiplier": 1.4,   # 凌晨干活耗能倍率
        "stress_multiplier": 1.2   # 凌晨干活增压倍率
    },
    "penalty_sleep_debt": {
        "drain_k": 0.05,           # 睡眠债对耗能的线性惩罚斜率 (1.0 + 0.05*debt)
        "stress_k": 0.04           # 睡眠债对增压的线性惩罚斜率
    },
    
    # ==========================================
    # 8. 夜间与日间恢复策略 (Rest & Night Strategy)
    # ==========================================
    "base_energy_recover": 2.2,
    "initial_phase_minutes": 60, 
    "initial_decay_rate": 0.02,  
    "oscillation_decay_normal": 0.015,  
    "oscillation_decay_deep": 0.02,    
    "anchor_threshold": 2.5,     
    "amplitude_base": 0.8,       
    "amplitude_growth_k": 0.04,  
    "low_stress_dampen": 0.5,    
    "max_amplitude": 3.5,        
    "friction_factor": 0.015,    
    "min_decay_rate": 0.006,
    "theta": 0.002,           
    "rest_noise_rho": 0.75, 
    "meal_lunch_recover": 10,       
    "meal_dinner_recover": 15,       
    "nap_short_recover": 12,         
    "nap_proper_recover": 20,    
    
    # ==========================================
    # 9. 连续负荷惩罚策略参数 (C_Strategy) 
    # ==========================================
    "c_strategy_params": {
        "high": {"threshold": 2.5, "rec_rate": 1.1, "k": 0.0005, "max_penalty": 0.0025, "exp": 1.5},
        "threshold": {"threshold": 3.0, "rec_rate": 1.2, "k": 0.0012, "max_penalty": 0.0020, "exp_k": -1.5},
        "low": {"threshold": 3.25, "rec_rate": 1.6, "k": 0.00025, "max_penalty": 0.0016}
    },

    # ==========================================
    # 10. 事件基础演化系数 (Event Priors & Intensities) 
    # ==========================================
    "D_t_course": 0.80,
    "D_t_task": 0.65,
    "course_base_drain": 8.5,      
    "task_base_drain": 8.5, 
    
    "event_task": {
        "T1_exam": 1.1, "T2_ddl": 1.05, "T3_meeting": 1.0,
        "T4_homework": 0.95, "T5_general": 0.90,
        "emotion_noise_low": 0.95,
        "emotion_noise_high": 1.05
    },
    
    "event_gym": {
        "drain_rate": 5.5,
        "fatigue_weight_k": -2.0,  
        "epoc_base": 1.5,          
        "epoc_k": 2.0,             
        "relief_factor": 0.02,
        "noise_s_base": 0.15,      
        "noise_s_k": 0.10,         
        "noise_e_base": 0.10,      
        "noise_e_k": 0.10          
    },
    
    "event_library": {
        "base_drain_rate": 0.72,
        "base_stress_rate": 0.15,
        "flow_relief_k": 0.008,
        "max_s_step": 1.5,
        "focus_base": 0.95,        
        "focus_decay_rate": 0.12,  
        "focus_min": 0.40,         
        "trait_weight_base": 0.4,  
        "trait_weight_k": 0.4      
    },
    "event_meal": {
        "C_base": 0.04,
        "K": 5.0,
        "multiplier_normal": 1.15,
        "multiplier_late": 0.85,
        "epoc_injection": 0.5,
        "epoc_max": 20.0,
        "duration_accel": 1.5
    },
    "event_nap": {
        "C_base": 0.08,
        "K": 5.0,
        "multiplier_proper": 1.4,
        "multiplier_short": 1.1,
        "debt_reduce_k": 2.0,
        "debt_multiplier": 1.2,
        "epoc_injection": 1.0,
        "epoc_max": 30.0,
        "duration_accel": 2.0
    },

    # ==========================================
    # 11. 状态机与流转惩罚 (State Machine) 
    # ==========================================
    "state_machine_fines": {
        "interrupt_S_penalty": 2.0,   
        "interrupt_E_penalty": -5.0,  
        "sleep_eff_base": 0.8,        
        "sleep_eff_drop": 0.1,        
        "sleep_eff_min": 0.5          
    },

    # ==========================================
    # 12. 细分策略统计学先验参数 (Strategy Statistical Priors)
    # ==========================================
    "night_normal": {"rho": 0.60, "sigma": 0.25, "pull_coeff": 0.035},
    "night_deep": {"rho": 0.80, "sigma": 0.15, "pull_coeff": 0.05},
    "night_anxious": {"rho": 0.55, "sigma": 0.22, "pull_coeff": 0.025},
    
    "rest_relieved": {"phase_thresholds": [2.0, 5.0], "efficiency": 1.05, "noise_std": 0.12, "inertia_e_rate": -0.05},
    "rest_warmup": {"phase_thresholds": [5.0, 15.0], "efficiency": 1.0, "noise_std": 0.10, "inertia_e_rate": -0.05},
    "rest_anxious": {"phase_thresholds": [10.0, 15.0], "efficiency": 0.85, "noise_std": 0.15, "inertia_e_rate": -0.06},
    "rest_burnout": {"phase_thresholds": [5.0, 10.0], "efficiency": 0.70, "noise_std": 0.04, "inertia_e_rate": -0.05},

    # ==========================================
    # 13. 压力敏感度曲线基准参数 (f_strategy_params)
    # ==========================================
    "f_strategy_params": {
        "sensitive": {"base": 0.40, "max_extra": 0.70, "midpoint": 15.0, "steepness": 0.20},
        "dull": {"base": 0.28, "threshold": 12.0, "k": 0.012},
        "saturated": {"floor": 0.30, "max_capacity": 1.0, "decay_midpoint": 25.0, "alpha": 0.15},
        "batterydrain": {"e_k": 0.4, "e_b": 5.0, "steepness": 0.15, "base": 0.30, "max_extra": 1.85}
    },
    
    # ==========================================
    # 14. 时间偏好映射权重 (Time Preferences)
    # ==========================================
    "time_pref_weights": {
        "like_morning": {"(8,10)": 0.95, "(10,12)": 0.9},
        "dislike_morning": {"(8,10)": 1.1, "(10,12)": 1.05},
        "like_afternoon": {"(12,14)": 0.95, "(14,16)": 0.95, "(16,18)": 1.0},
        "dislike_afternoon": {"(12,14)": 1.15, "(14,16)": 1.10, "(16,18)": 1.05},
        "like_evening": {"(18,20)": 0.9, "(20,24)": 0.9},
        "dislike_evening": {"(18,20)": 1.05, "(20,24)": 1.10}
    },
    # ==========================================
    # 15. 预警监控生态阈值 (Alert Thresholds) - 完整双轨引擎版
    # ==========================================
    "alert_thresholds": {
        "auc_limit": 100.0,              # 疲劳积分理论上限
        "critical_buffer_ratio": 0.35,   # 红警：瞬间击穿缓冲带的比例
        "warning_buffer_ratio": 0.20,    # 黄警：逼近警戒带的比例
        "auc_orange": 80.0,              # 橙警：疲劳积分绝对高危线
        "auc_yellow": 50.0,              # 黄警：疲劳积分警戒线
        "E_danger": 25.0,                # 残血判定线 (低精力易触发橙警)
        "auc_increase_step": 1.5,        # 持续高压时的单步积分增量
        "auc_decay_step": 2.5            # 脱离高压后的单步积分衰减
    },

    # ==========================================
    # 16. 智能日程编织参数 (Routine Weaver Priors) - 完整时空切片版
    # ==========================================
    "routine_weaver": {
        "max_delay_wake_time": "11:00",  
        "ideal_sleep_hours": 8.0,        
        "lunch_window_start": "11:00",   
        "lunch_window_end": "13:30",     
        "lunch_ideal_start": "11:40",
        "lunch_ideal_end": "12:20",
        "dinner_window_start": "17:00",
        "dinner_window_end": "19:30",
        "dinner_ideal_start": "17:40",
        "dinner_ideal_end": "18:30",
        "nap_debt_threshold": 0.5,       
        "nap_ideal_debt": 90,            
        "nap_ideal_normal": 40,          
        "nap_min_debt": 20,              
        "nap_min_normal": 15,            
        "nap_delay_after_lunch": 10,     
        "nap_window_end": "13:50"        
    }
}