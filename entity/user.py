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
from algorithm.mental_models import calculate_resilience_index
from algorithm.dynamic_state_model import MODEL_VARIANTS, normalize_model_variant
from settings.model_defaults import BASE_DATA_DIR, USER_CONFIG_DIR_NAME
from settings.parameter_store import get_param as resolve_param

USER_CONFIG_DIR = os.path.join(BASE_DATA_DIR, USER_CONFIG_DIR_NAME)

class User:
    """承载 params 深拷贝、三类策略、Simulator、睡眠债与 EPOC；配置默认不落盘。"""
    def __init__(self, user_id: str = "default", params: Optional[Dict[str, Any]] = None, load_from_file: bool = True):
        """
        参数:
            user_id: 标识；params: 覆盖 GLOBAL_DEFAULT_CONFIG 的键值；
            load_from_file: 为 True 时仍可能走 _load_config（当前实现返回 None）。
        """
        self.user_id = user_id
        
        self.params = copy.deepcopy(GLOBAL_DEFAULT_CONFIG)
        
        if load_from_file:
            saved_params = self._load_config()
            if saved_params:
                self.params.update(saved_params)
                
        if params:
            self.params.update(params)

        selection = self.params.get("model_selection", {})
        if not isinstance(selection, dict):
            selection = {}
        selection_status = str(selection.get("status") or "")
        if selection_status not in {
            "retained_from_empirical_evidence",
            "research_candidate_run",
        }:
            active_variant = "m0"
        else:
            active_variant = normalize_model_variant(
                selection.get("active_variant", "m0")
            )
        self.params["model_family"] = MODEL_VARIANTS[active_variant]["canonical"]
        self.params.setdefault("model_selection", {}).update(
            {"active_variant": active_variant}
        )
            
        self.solver: Optional['StressSolver'] = None
        self.night_strategy: Optional[NightStrategy] = None
        self.course_strategy: Optional[CourseStrategy] = None
        self.rest_strategy: Optional[RestStrategy] = None
        
        self.current_sleep_debt = 0.0
        
        self.resilience_index = 0.0
        self.epoc_level = 0.0
        
        self._refresh_strategy_runtime()
        self._init_solver()
    
    # =======================================================
    # === 睡眠债状态管理 (Sleep Debt Ecosystem) ===
    # =======================================================
    def set_sleep_debt(self, debt_hours: float):
        """睡眠债（小时），影响 drain/增压与部分事件逻辑。"""
        self.current_sleep_debt = max(0.0, float(debt_hours))
        
    def reduce_sleep_debt(self, hours: float):
        """偿还 hours 小时睡眠债，下限 0。"""
        self.current_sleep_debt = max(0.0, self.current_sleep_debt - hours)
        
    def get_sleep_debt(self) -> float:
        """当前睡眠债（小时）。"""
        return self.current_sleep_debt

    # =======================================================
    # === 极慢速双轨生态演化引擎 (Dual-Track Evolution) ===
    # =======================================================
    
    def get_current_S_star(self) -> float:
        """压力稳态锚 S*（与 config 中 S_star_init 一致）。"""
        return float(self.params.get("S_star_init", 50.0))
        
    def get_current_threshold(self) -> float:
        """Return the current care observation line for the active model."""
        if "ctssm" in str(self.params.get("model_family", "")).lower():
            alert_cfg = self.params.get("alert_thresholds", {})
            if isinstance(alert_cfg, dict) and "yellow_stress" in alert_cfg:
                return max(
                    float(alert_cfg["yellow_stress"]),
                    self.get_current_S_star() + 12.0,
                )
        return float(self.params.get("S_threshold", 90.0))
        
    def set_stress_baseline(self, s_star: float, threshold: float = None):
        """外部强制设定 S* 与可选阈值（会 save_config）。"""
        self.params["S_star_init"] = max(30.0, min(70.0, float(s_star)))
        if threshold is not None:
            self.params["S_threshold"] = min(110.0, float(threshold))
        
        self.save_config()
        print(f"用户 S* 被设定为: {self.params['S_star_init']:.2f}, 阈值: {self.params['S_threshold']:.2f}")

    def evolve_daily_baseline(self, wake_s: float, daily_mean_stress: float, has_red_alert: bool):
        """
        一日结束后更新 S_star_init 与 S_threshold（evolution_params）。
        参数:
            wake_s: 清晨唤醒时压力；daily_mean_stress: 当日 S 均值；has_red_alert: 是否出现最高级告警。
        """
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
            print(f"恶性磨损：红警或睡眠债过高({sleep_debt:.1f}h)，抗压天花板下降")
        elif daily_mean_stress > old_s_star + challenge_gap:
            # 良性锻炼：走出舒适区且安全度过
            new_threshold += evo_cfg.get("threshold_growth_benign", 0.10)
            print(f"良性锻炼：日均S={daily_mean_stress:.1f}，抗压天花板抬升")
        else:
            # 舒适区退化：缺乏压力刺激
            new_threshold -= evo_cfg.get("threshold_rust_comfort", 0.05)
            print(f"舒适区退化：日均S={daily_mean_stress:.1f}，天花板轻微下降")
            
        new_threshold = max(new_s_star + 20.0, min(110.0, new_threshold))
        
        self.params["S_star_init"] = round(new_s_star, 3)
        self.params["S_threshold"] = round(new_threshold, 3)
        
        print(f"S* {old_s_star:.2f} -> {new_s_star:.2f} | 阈值 {old_threshold:.2f} -> {new_threshold:.2f}")
        self.save_config()

    # =======================================================
    # === 内部初始化与工具函数 ===
    # =======================================================

    def _init_strategies(self):
        """按 params 重建 night/course/rest 策略并刷新 resilience_index。"""
        legacy = self.params.get("legacy_model", {})
        if not isinstance(legacy, dict):
            legacy = {}
        night_type = legacy.get(
            "night_strategy", self.params.get("night_strategy", "normal")
        )
        course_f_type = legacy.get(
            "f_strategy", self.params.get("f_strategy", "sensitive")
        )
        course_C_type = legacy.get(
            "C_strategy", self.params.get("C_strategy", "high")
        )
        rest_type = legacy.get(
            "rest_strategy", self.params.get("rest_strategy", "relieved")
        )
        time_prefs = legacy.get(
            "time_preferences", self.params.get("time_preferences", [])
        )
        
        self.night_strategy = NightStrategy.create(night_type, self.params)
        self.course_strategy = CourseStrategy.create(course_f_type, course_C_type, time_prefs, self.params)
        self.rest_strategy = RestStrategy.create(rest_type, self.params)
        
        self._calculate_resilience_index()
    
    def _refresh_strategy_runtime(self):
        """Keep legacy strategy objects out of the paper-aligned CTSSM path."""
        if "ctssm" not in str(self.params.get("model_family", "")).lower():
            self._init_strategies()
            return
        self.night_strategy = None
        self.course_strategy = None
        self.rest_strategy = None
        self.resilience_index = 0.0

    def _init_solver(self):
        """构造 Simulator(self)。"""
        from core_engine.simulator import Simulator
        self.solver = Simulator(self)

    def _calculate_resilience_index(self):
        """
        Recompute the bounded trait-resilience score from strategy selections.

        The mapping lives in ``algorithm.mental_models`` so psychology-facing
        scoring can be reviewed and tuned independently from persistence and
        strategy lifecycle concerns.
        """
        self.resilience_index = calculate_resilience_index(
            self.get_f_strategy(),
            self.get_C_strategy(),
            self.get_night_strategy(),
            self.get_rest_strategy(),
        )

    def get_resilience_index(self) -> float:
        """特质韧性指数。"""
        return self.resilience_index
    
    def update_params(self, new_params: Dict[str, Any]):
        """合并 new_params，重建策略并同步 solver。"""
        self.params.update(new_params)
        self._refresh_strategy_runtime()
        if self.solver:
            self.solver.update_user(self)
        self.save_config()  
    
    def get_param(self, key: str, default: Any = None):
        """Read a merged parameter with alias and central fallback support."""
        return resolve_param(self.params, key, default)
    
    def set_night_strategy(self, strategy_type: str):
        self.night_strategy = NightStrategy.create(strategy_type, self.params)
        self.params.setdefault("legacy_model", {})["night_strategy"] = strategy_type
        self._calculate_resilience_index()
        self.save_config()  
    
    def set_course_strategy(self, f_type: str, C_type: str, time_prefs: list = None):
        time_prefs = time_prefs or self.params.get("time_preferences", [])
        self.course_strategy = CourseStrategy.create(f_type, C_type, time_prefs, self.params)
        legacy = self.params.setdefault("legacy_model", {})
        legacy["f_strategy"] = f_type
        legacy["C_strategy"] = C_type
        self.params["time_preferences"] = time_prefs
        self._calculate_resilience_index()
        self.save_config()  
    
    def set_rest_strategy(self, strategy_type: str):
        self.rest_strategy = RestStrategy.create(strategy_type, self.params)
        self.params.setdefault("legacy_model", {})["rest_strategy"] = strategy_type
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
        """已阻断所有本地文件落盘操作，修改仅存于内存"""
        return None
    
    def _load_config(self) -> Optional[Dict[str, Any]]:
        """强制阻断本地读取，确保严格从 config.py 初始化"""
        return None
    
    def get_f_strategy(self) -> str:
        return self.params.get("legacy_model", {}).get(
            "f_strategy", self.params.get("f_strategy", "sensitive")
        )
    
    def get_C_strategy(self) -> str:
        return self.params.get("legacy_model", {}).get(
            "C_strategy", self.params.get("C_strategy", "high")
        )
    
    def get_night_strategy(self) -> str:
        return self.params.get("legacy_model", {}).get(
            "night_strategy", self.params.get("night_strategy", "normal")
        )
    
    def get_rest_strategy(self) -> str:
        return self.params.get("legacy_model", {}).get(
            "rest_strategy", self.params.get("rest_strategy", "relieved")
        )
    
    def get_time_preferences(self) -> List[str]:
        return self.params.get("time_preferences", [])
    
    def set_time_preference(self, preference: str, enabled: bool = True):
        time_prefs = self.get_time_preferences()
        if enabled and preference not in time_prefs:
            time_prefs.append(preference)
        elif not enabled and preference in time_prefs:
            time_prefs.remove(preference)
        self.params["time_preferences"] = time_prefs
        self._refresh_strategy_runtime()
        self.save_config()  
    
    def update_strategy_config(self, f_strategy: str = None, C_strategy: str = None,
                              night_strategy: str = None, rest_strategy: str = None,
                              time_preferences: List[str] = None):
        """仅更新策略相关键并 _init_strategies；与 update_params 可配合使用。"""
        legacy = self.params.setdefault("legacy_model", {})
        if f_strategy:
            legacy["f_strategy"] = f_strategy
        if C_strategy:
            legacy["C_strategy"] = C_strategy
        if night_strategy:
            legacy["night_strategy"] = night_strategy
        if rest_strategy:
            legacy["rest_strategy"] = rest_strategy
        if time_preferences is not None:
            legacy["time_preferences"] = time_preferences
        if time_preferences is not None:
            self.params["time_preferences"] = time_preferences
        self._refresh_strategy_runtime()
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
