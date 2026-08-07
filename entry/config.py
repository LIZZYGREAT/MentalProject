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
    "S_threshold": 70.0,  # 关怀观察线；仍需满足持续时间，不是临床阈值
    "E_critical": 25.0,   # 主观活力偏低参考线
    "time_step": 5,       # 仿真步长
    "random_seed": 42,
    # M0 remains active until out-of-time EMA comparison justifies a richer
    # candidate.  M1/M2/M3 are available to the research evaluation pipeline.
    "model_family": "stress-ctssm.m0",
    "model_selection": {
        "active_variant": "m0",
        "status": "insufficient_real_world_evidence",
        "minimum_test_days": 7,
        "minimum_relative_mae_improvement": 0.03,
        "minimum_interval_coverage": 0.80,
        "require_care_frequency_validation": True,
        "target_interval_coverage": 0.90,
    },
    # Phase 0 baseline: keep the model deterministic and identifiable.
    # Disabled mechanisms stay available for later ablation experiments, but
    # cannot influence the production baseline unless explicitly enabled.
    "feature_flags": {
        "enable_regime_switching": False,
        "enable_poisson_anomaly": False,
        "enable_friction_pool": False,
        "enable_epiphany_refund": False,
        "enable_dopamine_buffer": False,
        "enable_stress_momentum": False,
        "enable_daily_baseline_evolution": False,
        "enable_micro_dynamics": False,
    },
    "default_wake_time": "07:30",
    "default_sleep_time": "23:30",
    "noise_scale_factor": 0.2,
    "max_delta_base": 3.0,
    
    # ==========================================
    # 4. 课程与疲劳消耗参数 
    # ==========================================
    "alpha_cis_drain": 1.0,
    "cognitive_weight": 0.65,
    "fatigue_acceleration": 0.15,
    "fatigue_acceleration_k": 0.15,
    
    # ==========================================
    # 5. 旧模型兼容参数（不会进入 CTSSM）
    # ==========================================
    "legacy_model": {
        "enabled": False,
        "f_strategy": "sensitive",
        "C_strategy": "high",
        "night_strategy": "normal",
        "rest_strategy": "relieved",
        "time_preferences": [],
    },
    
    # ==========================================
    # 6. 非稳态负荷理论参数 (Allostatic Load Dynamics) 
    # ==========================================
    "allostatic_collapse_point": 0.35,     
    "allostatic_collapse_steepness": 10.0, 
    "allostatic_max_penalty": 0.25,         
    "allostatic_cost_alpha": 0.75,         
    "allostatic_cost_beta": 1.5,          
    
    # ==========================================
    # 7. 全局系统性惩罚参数 (Global Penalties) 
    # ==========================================
    "penalty_circadian": {
        "drain_multiplier": 1.4,   
        "stress_multiplier": 1.2  
    },
    "penalty_sleep_debt": {
        "drain_k": 0.05,           
        "stress_k": 0.04           
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
    "rhythm_jitter_amplitude": 1.5,
    "rhythm_jitter_period": 37.0,
    "friction_factor": 0.015,    
    "min_decay_rate": 0.006,
    "theta": 0.002,           
    "rest_noise_rho": 0.75, 
    "meal_lunch_recover": 10,       
    "meal_dinner_recover": 15,       
    "nap_short_recover": 12,         
    "nap_proper_recover": 20,    

    "rest_ode_params": {
        "R_max_base": 6.0,                  
        "deficit_gamma": 2.0,                
        "sympathetic_inhibit_alpha": 0.02,  
        "energy_noise_std": 0.05            
    },
    "recovery_piecewise_params": {
        "thresholds": [10.0, 40.0],           
        "absorption_rates": [1.0, 0.9, 0.7], 
        "relief_multipliers": [1.0, 1.3, 0.7] 
    },
    "rest_trait_modifiers": {
        "relieved": {"eta": 1.05, "tau": 0.7},   
        "warmup":   {"eta": 1.00, "tau": 1.5},  
        "anxious":  {"eta": 0.90, "tau": 1.0},   
        "burnout":  {"eta": 0.80, "tau": 1.0},   
        "default":  {"eta": 1.00, "tau": 1.0}
    },
    
    # ==========================================
    # 9. 连续负荷惩罚策略参数 (C_Strategy)
    # ==========================================
    "c_strategy_params": {
        "high": {"threshold": 2.75, "rec_rate": 1.25, "k": 0.0025, "max_penalty": 0.0040, "exp": 1.35},
        "threshold": {"threshold": 3.0, "rec_rate": 1.5, "k": 0.0275, "max_penalty": 0.0040, "exp_k": -1.5},
        "low": {"threshold": 3.5, "rec_rate": 1.6, "k": 0.0020, "max_penalty": 0.0035}
    },

    # ==========================================
    # 10. 事件基础演化系数 (Event Priors & Intensities) 
    # ==========================================
    "D_t_course": 0.80,
    "D_t_task": 0.55,
    "course_base_drain": 5.5,      
    "task_base_drain": 5.0, 
    "base_task_drain": 5.0,
    
    "event_task": {
        "T1_exam": 1.1, "T2_ddl": 1.05, "T3_meeting": 1.0,
        "T4_homework": 0.95, "T5_general": 0.90,
        "weight_exam": 1.10,
        "weight_ddl": 1.05,
        "weight_meeting": 0.85,
        "weight_homework": 0.95,
        "weight_general": 0.90,
        "emotion_noise_low": 0.95,
        "emotion_noise_high": 1.05
    },
    
    "event_gym": {
        "drain_rate": 5.5,
        "fatigue_weight_k": -2.0,  
        "epoc_base": 1.5,          
        "epoc_k": 2.0,             
        "epoc_rate": 0.05,
        "relief_factor": 0.02,
        "noise_s_base": 0.15,      
        "noise_s_k": 0.10,         
        "noise_e_base": 0.10,      
        "noise_e_k": 0.10          
    },
    
    "event_library": {
        "base_drain_rate": 0.75,
        "base_stress_rate": 0.60,
        "flow_relief_k": 0.020,
        "max_s_step": 1.2,
        "focus_base": 0.95,        
        "focus_decay_rate": 0.125,  
        "focus_min": 0.45,         
        "trait_weight_base": 0.4,  
        "trait_weight_k": 0.4      
    },


    "event_meal": {
        "A_max": 1.0,              
        "K_half": 15.0,             
        "hill_n": 2.0,              
        "logistic_k": 0.15,         
        "logistic_mid": 25.0,       
        "logistic_min": 0.75,       
        "time_damp_b": 0.3,         
        "time_damp_lambda": 2.0,    
        "multiplier_normal": 1.15,
        "multiplier_late": 0.85,
        "epoc_injection": 0.5,
        "epoc_max": 20.0
    },
    
    "event_nap": {
        "A_max": 1.25,             
        "K_half": 15.0,             
        "hill_n": 2.0,              
        "logistic_k": 0.15,         
        "logistic_mid": 25.0,       
        "logistic_min": 0.75,       
        "time_damp_b": 0.3,         
        "time_damp_lambda": 2.0,    
        "multiplier_proper": 1.4,
        "multiplier_short": 1.1,
        "debt_reduce_k": 2.0,
        "debt_multiplier": 1.2,
        "epoc_injection": 1.0,
        "epoc_max": 30.0
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
    "night_normal": {"rho": 0.70, "sigma": 0.090, "pull_coeff": 0.035},
    "night_deep": {"rho": 0.80, "sigma": 0.075, "pull_coeff": 0.05},
    "night_anxious": {"rho": 0.65, "sigma": 0.10, "pull_coeff": 0.025},
    
    "rest_relieved": {"phase_thresholds": [2.0,10.0], "efficiency": 1.05, "noise_std": 0.12, "inertia_e_rate": -0.05},
    "rest_warmup": {"phase_thresholds": [5.0, 15.0], "efficiency": 1.0, "noise_std": 0.10, "inertia_e_rate": -0.05},
    "rest_anxious": {"phase_thresholds": [10.0, 15.0], "efficiency": 0.85, "noise_std": 0.15, "inertia_e_rate": -0.06},
    "rest_burnout": {"phase_thresholds": [5.0, 10.0], "efficiency": 0.70, "noise_std": 0.04, "inertia_e_rate": -0.05},

    # ==========================================
    # 13. 压力敏感度曲线基准参数 (f_strategy_params)
    # ==========================================
    "f_strategy_params": {
        "sensitive": {"base": 0.80, "max_extra": 0.30, "midpoint": 17.5, "steepness": 0.15},
        "dull": {"base": 0.50, "threshold": 12.0, "k": 0.012},
        "saturated": {"floor": 0.65, "max_capacity": 1.0, "decay_midpoint": 15.0, "alpha": 0.15},
        "batterydrain": {"e_k": 0.15, "e_b": 2, "steepness": 0.4, "base": 0.45, "max_extra": 0.8}
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
    # 15. 预警监控生态阈值 (Alert Thresholds) 
    # ==========================================
    "alert_thresholds": {
        "yellow_stress": 70.0,
        "orange_stress": 80.0,
        "red_stress": 88.0,
        "extreme_stress": 94.0,
        "recovery_stress": 62.0,
        "yellow_confirm_minutes": 40.0,
        "orange_confirm_minutes": 20.0,
        "red_confirm_minutes": 10.0,
        "rearm_minutes": 45.0,
        "cooldown_minutes": 180.0,
        "escalation_cooldown_minutes": 90.0,
        "critical_cooldown_minutes": 90.0,
        "max_daily_care": 2,
        "max_daily_critical_override": 1,
        "elevated_auc_yellow": 2.2,
        "elevated_auc_orange": 3.6,
        "elevated_auc_red": 5.5,
        "E_danger": 25.0,
    },

    # ==========================================
    # 16. 理论引导连续时间潜在状态候选 (M0-M3；默认仅 M0)
    # 所有速率统一使用“每小时”量纲；日历评价是先验，不是测量值。
    # ==========================================
    "ctssm_params": {
        "stress_reactivity_per_hour": 1.55,
        "stress_recovery_per_hour": 0.68,
        "event_stress_gain": 30.0,
        "m0_anticipation_stress_gain": 5.0,
        "m0_post_event_stress_gain": 8.0,
        "cognition_stress_gain": 15.0,
        "fatigue_stress_gain": 17.0,
        "sleep_debt_stress_per_hour": 1.2,
        "vitality_baseline": 72.0,
        "vitality_regulation_per_hour": 0.58,
        "demand_vitality_drain_per_hour": 13.0,
        "recovery_vitality_gain_per_hour": 10.0,
        "fatigue_vitality_gain": 27.0,
        "sleep_debt_vitality_per_hour": 1.8,
        "cognition_decay_per_hour": 1.05,
        "anticipation_gain_per_hour": 0.90,
        "aftermath_gain_per_hour": 1.00,
        "fatigue_accumulation_per_hour": 0.42,
        "fatigue_recovery_per_hour": 0.95,
        "stress_vitality_coupling": "none",
        "vitality_to_stress_gain": 0.10,
        "stress_to_vitality_gain": 0.08,
        "cross_day_stress_persistence": 0.42,
        "cross_day_vitality_persistence": 0.38,
        "cross_day_cognition_persistence": 0.15,
        "cross_day_fatigue_persistence": 0.62,
        "cross_day_fatigue_stress_gain": 6.0,
        "cross_day_unfinished_decay_hours": 18.0,
        "cross_day_unfinished_input_floor": 0.18,
        "cross_day_unfinished_sleep_multiplier": 0.25,
        "sleep_quality_initial_stress_gain": 5.0,
        "sleep_quality_initial_vitality_gain": 7.0,
        "sleep_quality_event_appraisal_gain": 0.08,
        "stress_process_sd_per_sqrt_hour": 3.0,
        "vitality_process_sd_per_sqrt_hour": 3.5,
        "cognition_process_sd_per_sqrt_hour": 0.08,
        "fatigue_process_sd_per_sqrt_hour": 0.05,
        "initial_stress_variance": 100.0,
        "initial_vitality_variance": 100.0,
        "initial_cognition_variance": 0.04,
        "initial_fatigue_variance": 0.04,
        "stress_observation_sd": 8.0,
        "vitality_observation_sd": 9.0,
        "cognition_observation_sd": 0.18,
        "observation_delay_variance_per_hour": 0.55,
        "retrospective_variance_multiplier": 0.75,
        "stress_time_of_day": [
            (0, -2.0), (7, -1.0), (10, 0.0), (14, 1.5),
            (18, 2.0), (22, 1.0), (24, -2.0),
        ],
        "vitality_time_of_day": [
            (0, -8.0), (7, 1.0), (10, 5.0), (14, 1.0),
            (18, -2.0), (22, -7.0), (24, -9.0),
        ],
    },

    # ==========================================
    # 17. 智能日程编织参数 (Routine Weaver Priors)
    # ==========================================
    "routine_weaver": {
        "max_delay_wake_time": "11:00",  
        "ideal_sleep_hours": 8.0,        
        "lunch_window_start": "11:00",   
        "lunch_window_end": "13:30",     
        "lunch_ideal_start": "11:40",
        "lunch_ideal_end": "12:10",
        "dinner_window_start": "17:00",
        "dinner_window_end": "19:30",
        "dinner_ideal_start": "17:40",
        "dinner_ideal_end": "18:10",
        "nap_debt_threshold": 0.5,       
        "nap_ideal_debt": 90,            
        "nap_ideal_normal": 40,          
        "nap_min_debt": 20,              
        "nap_min_normal": 15,            
        "nap_delay_after_lunch": 10,     
        "nap_window_end": "13:50",
        "meal_min_duration": 20,
        "meal_normal_min_duration": 30,
        "sleep_transition_buffer": 15,
        "next_event_buffer": 30,
        "ignore_early_midnight_before": 150
    },

    # ==========================================
    # 18. 引擎微观化学动力学与滤波器常数 (Micro-Dynamics & Chemical Pools)
    # ==========================================
    "simulator_micro_params": {
        "wandering_cooldown_base": 40,          
        "wandering_cooldown_var": (-10, 15),    
        "epiphany_refund_ratio": 0.8,           
        "epiphany_refund_max": 8.0,             
        "epiphany_base_bonus": 2.0,             
        "epiphany_resilience_k": 1.5,           
        "dopamine_leak_rate": 0.15,             
        "epoc_absorption_rate": 1.5,            
        "epoc_abs_e_base": 0.6,                 
        "epoc_abs_e_k": 0.2,                    
        "epoc_abs_s_base": 0.08,                
        "epoc_abs_s_k": 0.05,                   
        "momentum_beta": 0.10,                  
        "concurrent_log_base": 0.3,             
        "energy_exhaustion_threshold": 20.0,   
        "rest_penalty_recovery_delay": 5.0,
        "friction_decay_rate": 0.2,
        "dopamine_release_rate": 0.15,
        "dopamine_energy_ratio": 0.4,
        "basal_stress_gap_k": 0.02,

        "buffer_decay_rate": 0.05,           
        "basal_drain_rate": 0.415,           
        "lorentzian_floor_E": 15.0,
    },

    # ==========================================
    # 19. 宏观双轨生态演化常数 (Macro Ecological Evolution)
    # ==========================================
    "evolution_params": {
        "alpha_star": 0.015,                    
        "threshold_wear_malignant": 0.25,       
        "threshold_growth_benign": 0.10,        
        "threshold_rust_comfort": 0.05,         
        "benign_challenge_gap": 10.0,           
        "malignant_debt_limit": 1.5             
    },
    
    # ==========================================
    # 20. 任务刺激习惯化参数 (Stimulus Habituation)
    # ==========================================
    "habituation_params": {
        "decay_model": "hyperbolic",  
        "floor_mu_course": 0.35,       
        "floor_mu_task": 0.35,         
        "t_half_hyperbolic": 40.0      
    },

    # ==========================================
    # 21. 半马尔可夫情绪状态机与势能参数 (Semi-Markov Dynamics)
    # ==========================================
    "markov_semi_params": {
        "regime_check_interval": 25,     
        "w_fatigue": 0.15,               
        "w_debt": 0.30,                  
        "w_intensity": 0.50,             
        "w_resilience": 0.40,            
        "shield_library": 0.20,          
        "shield_gym": 0.50,              
        "shield_rest": 0.30,             
        "k_shape": 1.5,                  
        "lambda_base_flow": 0.02,        
        "lambda_base_friction": 0.015,   
        "lambda_base_normal": 0.01,      
        "gamma_flow": 2.0,               
        "gamma_friction": 1.5,           
        "gamma_normal": 1.2,             
        "kappa_logit": 3.0,              
        "poisson_anomaly_prob": 0.01     
    },

    # ==========================================
    # 22. 动态区制乘数边界 (Dynamic Regime Modifiers)
    # ==========================================
    "markov_modifiers": {
        "friction_s_base": 1.05,         
        "friction_s_max": 1.15,          
        "friction_e_base": 1.15,         
        "flow_s_base": 0.95,             
        "flow_s_min": 0.85,              
        "flow_e_base": 0.85              
    }
}
