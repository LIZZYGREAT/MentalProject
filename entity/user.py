# user.py
import os
import json
import copy
from typing import Dict, Any, Optional, TYPE_CHECKING, List

if TYPE_CHECKING:
    from core_engine.simulator import StressSolver
    
from strategy.night_strategy import NightStrategy
from strategy.course_strategy import CourseStrategy
from strategy.rest_strategy import RestStrategy

from entry.config import GLOBAL_DEFAULT_CONFIG

BASE_DATA_DIR = "data"
USER_CONFIG_DIR = os.path.join(BASE_DATA_DIR, "user_configs")

class User:
    def __init__(self, user_id: str = "default", params: Optional[Dict[str, Any]] = None, load_from_file: bool = True):
        self.user_id = user_id
        
        # 强制深拷贝 config.py 作为唯一真理来源
        self.params = copy.deepcopy(GLOBAL_DEFAULT_CONFIG)
        
        # 即使 load_from_file 为 True，内部已被阻断，确保无状态
        if load_from_file:
            saved_params = self._load_config()
            if saved_params:
                self.params.update(saved_params)
                
        if params:
            self.params.update(params)
            
        self.solver: Optional['StressSolver'] = None
        self.night_strategy: Optional[NightStrategy] = None
        self.course_strategy: Optional[CourseStrategy] = None
        self.rest_strategy: Optional[RestStrategy] = None
        
        self.current_sleep_debt = 0.0
        
        self.resilience_index = 0.0
        self.epoc_level = 0.0
        
        self._init_strategies()
        self._init_solver()
    
    # =======================================================
    # === 睡眠债状态管理 (Sleep Debt Ecosystem) ===
    # =======================================================
    def set_sleep_debt(self, debt_hours: float):
        self.current_sleep_debt = max(0.0, float(debt_hours))
        
    def reduce_sleep_debt(self, hours: float):
        self.current_sleep_debt = max(0.0, self.current_sleep_debt - hours)
        
    def get_sleep_debt(self) -> float:
        return self.current_sleep_debt

    # =======================================================
    # === 极慢速双轨生态演化引擎 (Dual-Track Evolution) ===
    # =======================================================
    
    def get_current_S_star(self) -> float:
        return float(self.params.get("S_star_init", 50.0))
        
    def get_current_threshold(self) -> float:
        return float(self.params.get("S_threshold", 90.0))
        
    def set_stress_baseline(self, s_star: float, threshold: float = None):
        self.params["S_star_init"] = max(30.0, min(70.0, float(s_star)))
        if threshold is not None:
            self.params["S_threshold"] = min(110.0, float(threshold))
        
        self.save_config()
        print(f"🔧 [干预] 用户 S* 被强制设定为: {self.params['S_star_init']:.2f}, 阈值: {self.params['S_threshold']:.2f}")

    def evolve_daily_baseline(self, wake_s: float, daily_mean_stress: float, has_red_alert: bool):
        """核心演进接口：极慢速异位稳态演算"""
        old_s_star = self.get_current_S_star()
        old_threshold = self.get_current_threshold()
        sleep_debt = self.get_sleep_debt()
        
        evo_cfg = self.params.get("evolution_params", {})
        
        # === 轨线一：S* 静息底线漂移 (基于清晨唤醒状态) ===
        alpha_star = evo_cfg.get("alpha_star", 0.015)
        new_s_star = old_s_star + alpha_star * (wake_s - old_s_star)
        new_s_star = max(40.0, min(70.0, new_s_star))
        
        # === 轨线二：Threshold 破防天花板磨损 (基于全天挑战) ===
        new_threshold = old_threshold
        
        debt_limit = evo_cfg.get("malignant_debt_limit", 1.5)
        challenge_gap = evo_cfg.get("benign_challenge_gap", 10.0)
        
        if has_red_alert or sleep_debt > debt_limit:
            # 恶性磨损：防线击穿或高睡眠债反噬
            new_threshold -= evo_cfg.get("threshold_wear_malignant", 0.25)
            print(f"💔 [生态演化] 恶性磨损：触发红警或睡眠债过高({sleep_debt:.1f}h)，抗压天花板下降")
        elif daily_mean_stress > old_s_star + challenge_gap:
            # 良性锻炼：走出舒适区且安全度过
            new_threshold += evo_cfg.get("threshold_growth_benign", 0.10)
            print(f"💪 [生态演化] 良性锻炼：抗住高压挑战(日均S={daily_mean_stress:.1f})，抗压天花板抬升")
        else:
            # 舒适区退化：缺乏压力刺激
            new_threshold -= evo_cfg.get("threshold_rust_comfort", 0.05)
            print(f"🛋️ [生态演化] 舒适区生锈：缺乏压力刺激(日均S={daily_mean_stress:.1f})，天花板轻微下降")
            
        new_threshold = max(new_s_star + 20.0, min(110.0, new_threshold))
        
        self.params["S_star_init"] = round(new_s_star, 3)
        self.params["S_threshold"] = round(new_threshold, 3)
        
        print(f"🌱 [双轨演化结算] S* 底线: {old_s_star:.2f} -> {new_s_star:.2f} | 报警阈值: {old_threshold:.2f} -> {new_threshold:.2f}")
        self.save_config()

    # =======================================================
    # === 内部初始化与工具函数 ===
    # =======================================================

    def _init_strategies(self):
        night_type = self.params.get("night_strategy", "normal")
        course_f_type = self.params.get("f_strategy", "sensitive")
        course_C_type = self.params.get("C_strategy", "high")
        rest_type = self.params.get("rest_strategy", "relieved")
        time_prefs = self.params.get("time_preferences", [])
        
        self.night_strategy = NightStrategy.create(night_type, self.params)
        self.course_strategy = CourseStrategy.create(course_f_type, course_C_type, time_prefs, self.params)
        self.rest_strategy = RestStrategy.create(rest_type, self.params)
        
        self._calculate_resilience_index()
    
    def _init_solver(self):
        from core_engine.simulator import Simulator
        self.solver = Simulator(self)

    def _calculate_resilience_index(self):
        """
        [全局性格提取] 提取用户的“特质韧性指数” (范围大致在 -1.0 到 1.0)
        供全系统(LibraryEvent, 运动EPOC吸收等)直接调用
        """
        score = 0.0
        
        f_strat = self.get_f_strategy()
        if f_strat == "dull": score += 0.3
        elif f_strat == "saturated": score += 0.2
        elif f_strat == "sensitive": score -= 0.3
        elif f_strat == "batterydrain": score -= 0.2
            
        c_strat = self.get_C_strategy()
        if c_strat == "low": score += 0.2
        elif c_strat == "threshold": score += 0.1
        elif c_strat == "high": score -= 0.2
            
        n_strat = self.get_night_strategy()
        if n_strat == "deep": score += 0.3
        elif n_strat == "normal": score += 0.0
        elif n_strat == "anxious": score -= 0.3
            
        r_strat = self.get_rest_strategy()
        if r_strat == "relieved": score += 0.2
        elif r_strat == "warmup": score += 0.1
        elif r_strat == "burnout": score -= 0.1
        elif r_strat == "anxious": score -= 0.2
            
        self.resilience_index = max(-1.0, min(1.0, score))

    def get_resilience_index(self) -> float:
        return self.resilience_index
    
    def update_params(self, new_params: Dict[str, Any]):
        self.params.update(new_params)
        self._init_strategies()
        if self.solver:
            self.solver.update_user(self)
        self.save_config()  
    
    def get_param(self, key: str, default: Any = None):
        return self.params.get(key, default)
    
    def set_night_strategy(self, strategy_type: str):
        self.night_strategy = NightStrategy.create(strategy_type, self.params)
        self.params["night_strategy"] = strategy_type
        self._calculate_resilience_index()
        self.save_config()  
    
    def set_course_strategy(self, f_type: str, C_type: str, time_prefs: list = None):
        time_prefs = time_prefs or self.params.get("time_preferences", [])
        self.course_strategy = CourseStrategy.create(f_type, C_type, time_prefs, self.params)
        self.params["f_strategy"] = f_type
        self.params["C_strategy"] = C_type
        self.params["time_preferences"] = time_prefs
        self._calculate_resilience_index()
        self.save_config()  
    
    def set_rest_strategy(self, strategy_type: str):
        self.rest_strategy = RestStrategy.create(strategy_type, self.params)
        self.params["rest_strategy"] = strategy_type
        self._calculate_resilience_index()
        self.save_config()  
    
    def get_config_file_path(self) -> str:
        os.makedirs(USER_CONFIG_DIR, exist_ok=True)
        return os.path.join(USER_CONFIG_DIR, f"{self.user_id}_config.json")
    
    @staticmethod
    def _params_to_json_safe(params: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for k, v in params.items():
            if k == "time_weights" and isinstance(v, dict):
                out[k] = {f"{a},{b}": val for (a, b), val in v.items()}
            else:
                out[k] = v
        return out
    
    @staticmethod
    def _params_from_json_safe(data: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for k, v in data.items():
            if k == "time_weights" and isinstance(v, dict):
                out[k] = {}
                for key_str, val in v.items():
                    try:
                        parts = key_str.split(",")
                        if len(parts) == 2:
                            out[k][(int(parts[0].strip()), int(parts[1].strip()))] = val
                        else:
                            out[k][key_str] = val
                    except (ValueError, TypeError):
                        out[k][key_str] = val
            else:
                out[k] = v
        return out
    
    def save_config(self):
        """ [沙盒模式] 已阻断所有本地文件落盘操作，修改仅存于内存 """
        print(f"✅ [无状态模式] 用户配置修改已在内存中生效，不会写入本地文件。")
    
    def _load_config(self) -> Optional[Dict[str, Any]]:
        """ [沙盒模式] 强制阻断本地读取，确保严格从 config.py 初始化 """
        print(f"ℹ️ [无状态模式] 忽略本地持久化配置加载，严格使用 config.py 基准。")
        return None
    
    def get_f_strategy(self) -> str:
        return self.params.get("f_strategy", "sensitive")
    
    def get_C_strategy(self) -> str:
        return self.params.get("C_strategy", "high")
    
    def get_night_strategy(self) -> str:
        return self.params.get("night_strategy", "normal")
    
    def get_rest_strategy(self) -> str:
        return self.params.get("rest_strategy", "relieved")
    
    def get_time_preferences(self) -> List[str]:
        return self.params.get("time_preferences", [])
    
    def set_time_preference(self, preference: str, enabled: bool = True):
        time_prefs = self.get_time_preferences()
        if enabled and preference not in time_prefs:
            time_prefs.append(preference)
        elif not enabled and preference in time_prefs:
            time_prefs.remove(preference)
        self.params["time_preferences"] = time_prefs
        self._init_strategies()
        self.save_config()  
    
    def update_strategy_config(self, f_strategy: str = None, C_strategy: str = None,
                              night_strategy: str = None, rest_strategy: str = None,
                              time_preferences: List[str] = None):
        if f_strategy:
            self.params["f_strategy"] = f_strategy
        if C_strategy:
            self.params["C_strategy"] = C_strategy
        if night_strategy:
            self.params["night_strategy"] = night_strategy
        if rest_strategy:
            self.params["rest_strategy"] = rest_strategy
        if time_preferences is not None:
            self.params["time_preferences"] = time_preferences
        self._init_strategies()
        if self.solver:
            self.solver.update_user(self)
        self.save_config()  
    
    def print_config(self):
        print("\n当前用户配置:")
        print(f"  用户ID: {self.user_id}")
        print(f"  当前的稳态基准线 S*: {self.get_current_S_star():.2f}")
        print(f"  当前的报警阈值: {self.get_current_threshold():.2f}")
        print(f"  压力敏感函数策略: {self.get_f_strategy()}")
        print(f"  连续上课惩罚策略: {self.get_C_strategy()}")
        print(f"  夜间恢复策略: {self.get_night_strategy()}")
        print(f"  日间休息策略: {self.get_rest_strategy()}")
        time_prefs = self.get_time_preferences()
        if time_prefs:
            print(f"  时间偏好: {', '.join(time_prefs)}")
        else:
            print(f"  时间偏好: 无")