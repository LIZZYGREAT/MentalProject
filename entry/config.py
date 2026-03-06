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
    "fatigue_acceleration": 1.20,
    "cognitive_weight": 0.65,
    
    # ==========================================
    # 5. 用户默认策略偏好
    # ==========================================
    "f_strategy": "sensitive", 
    "C_strategy": "high",        
    "night_strategy": "normal",  
    "rest_strategy": "relieved",
    "time_preferences": [],
    
    # ==========================================
    # 6. 夜间睡眠策略常数 (Night Strategy)
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
    
    # ==========================================
    # 7. 日间休息策略常数 (Rest Strategy)
    # ==========================================
    "theta": 0.002,           
    "rest_noise_rho": 0.75,   

    # 8. 非课程类任务基准强度 (Task Intensities)
    # ==========================================
    "task_base_intensities": {
        "exam": 1.1,
        "ddl": 1.05,
        "meeting": 1.0,
        "homework": 0.95,
        "general": 0.90
    },

    # ==========================================
    # 9. 具体事件硬编码参数 (Event Parameters)
    # ==========================================
    # 回血类 
    "meal_lunch_recover": 10,       
    "meal_dinner_recover": 15,       
    "nap_short_recover": 12,         
    "nap_proper_recover": 20,      

    # 耗精类
    "lib_base_drain_rate": 0.72,     
    "gym_drain_rate": 5.5,            
    "course_base_drain": 8.5,      
    "task_base_drain": 8.5,          
    
    "lib_base_stress_rate": 0.15,     
    "lib_flow_relief_k": 0.008,       
    "lib_max_s_step": 1.5,            
    "gym_relief_factor": 0.02,        
    "gym_epoc_rate": 0.05,
    
}