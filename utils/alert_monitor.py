# alert_monitor.py
from typing import List, Dict, Tuple
from settings.model_defaults import (
    DEFAULT_ENERGY_CRITICAL,
    DEFAULT_INITIAL_ENERGY,
    DEFAULT_INITIAL_STRESS,
    DEFAULT_STRESS_THRESHOLD,
    RECOVERY_STATES,
)

class AlertMonitor:
    """
    双引擎：绝对水位（S 与阈值的相对位置）与 AUC 式持续高负荷积分；
    睡眠态降积分并重置阶梯。阈值来自 params['alert_thresholds'] 与 S_threshold/S_star_init。
    """
    def __init__(self, params: Dict):
        """
        参数 params: 全局配置字典，需含 S_threshold、S_star_init、alert_thresholds 等。
        """
        self.params = params
        self.S_thresh = params.get("S_threshold", DEFAULT_STRESS_THRESHOLD)
        self.S_star = params.get("S_star_init", DEFAULT_INITIAL_STRESS)
        
        alert_cfg = params.get("alert_thresholds", {})
        self.auc_limit = alert_cfg.get("auc_limit", 100.0)
        self.critical_buffer_ratio = alert_cfg.get("critical_buffer_ratio", 0.35)
        self.warning_buffer_ratio = alert_cfg.get("warning_buffer_ratio", 0.20)
        self.auc_orange = alert_cfg.get("auc_orange", 80.0)
        self.auc_yellow = alert_cfg.get("auc_yellow", 50.0)
        self.E_danger = alert_cfg.get("E_danger", DEFAULT_ENERGY_CRITICAL)
        self.auc_increase_step = alert_cfg.get("auc_increase_step", 1.5)
        self.auc_decay_step = alert_cfg.get("auc_decay_step", 2.5)
        self.sleep_auc_decay_step = alert_cfg.get("sleep_auc_decay_step", 5.0)
        
        # 计算抗压缓冲带
        self.buffer_zone = max(10.0, self.S_thresh - self.S_star)
        
    def analyze(self, results: List[Dict]) -> Tuple[List[Dict], List[float]]:
        """
        参数 results: simulate_day 输出的每步字典列表（含 S,E,state,delta_S,continuous_hours 等）。
        返回: (alerts, confidence_series)，后者与 results 对齐供绘图。
        """
        alerts = []
        confidence_series = []
        
        auc_level = 0.0
        current_alert_tier = 0  # 状态机：0: 无, 1: 黄, 2: 橙, 3: 红
        
        for row in results:
            S = row.get("S", 0.0)
            E = row.get("E", DEFAULT_INITIAL_ENERGY)
            state = row.get("state", "UNKNOWN")
            time_str = row.get("time", "00:00")
            delta_S = row.get("delta_S", 0.0)
            continuous_hours = row.get("continuous_hours", 0.0)
            current_events = row.get("current_events", [])
            dominant_stressors = row.get("dominant_stressors", [])
            
            # 1. 睡眠状态：降温并重置报警阶梯
            if state in RECOVERY_STATES:
                auc_level = max(0.0, auc_level - self.sleep_auc_decay_step)
                current_alert_tier = 0  
                confidence_series.append(0.0)
                continue
                
            # 2. 状态判定与积分更新
            is_resting_and_recovering = (delta_S < 0 and state not in ["LATE_NIGHT_ACTIVE", "NIGHT_OVERTIME"])
            
            # 只要压力大于黄警线（消耗了动态比例的可用空间），就开始涨积分（休息时除外）
            if S > self.S_thresh - self.warning_buffer_ratio * self.buffer_zone:
                if is_resting_and_recovering:
                    auc_level = max(0.0, auc_level - 1.0)
                else:
                    auc_level = min(self.auc_limit, auc_level + self.auc_increase_step)
            else:
                auc_level = max(0.0, auc_level - self.auc_decay_step)
                
            # ==========================================
            # 3. 双引擎独立判定
            # ==========================================
            
            # 引擎一：绝对值水位引擎 (Intensity Engine)
            intensity_tier = 0
            intensity_zone = "safe"
            
            if S >= self.S_thresh + self.critical_buffer_ratio * self.buffer_zone:
                intensity_tier = 3
                intensity_zone = "critical"
            elif S >= self.S_thresh:
                intensity_tier = 2
                intensity_zone = "breached"
            elif S >= self.S_thresh - self.warning_buffer_ratio * self.buffer_zone:
                intensity_tier = 1
                intensity_zone = "approaching"
                
            # 引擎二：疲劳积分引擎 (Duration Engine)
            duration_tier = 0
            if auc_level >= self.auc_limit:
                duration_tier = 3
            elif auc_level >= self.auc_orange or (auc_level >= self.auc_yellow and E < self.E_danger):
                duration_tier = 2
            elif auc_level >= self.auc_yellow:
                duration_tier = 1
                
            # 综合评定：取双引擎的最高危级别
            target_tier = max(intensity_tier, duration_tier)
            
            # 如果没有报警，直接记录置信度并进入下一步
            if target_tier == 0:
                confidence_series.append(0.0)
                continue
                
            # ==========================================
            # 4. 生成报警话术与归因
            # ==========================================
            alert_text = ""
            trigger_source = ""
            
            # 优先判定是否由强度瞬间刺穿触发
            if intensity_tier >= duration_tier and intensity_tier > 0:
                trigger_source = "intensity_spike"
                if intensity_tier == 3:
                    alert_text = "[红] 极度高压 (瞬间峰值穿透绝对底线)"
                elif intensity_tier == 2:
                    alert_text = "[橙] 防线击穿 (高压突破当前容忍阈值)"
                else:
                    alert_text = "[黄] 承压预警 (压力逼近警戒带)"
            else:
                trigger_source = "duration_buildup"
                if duration_tier == 3:
                    alert_text = "[红] 阈值过载 (持续高负荷导致系统崩溃)"
                elif duration_tier == 2:
                    if E < self.E_danger:
                        alert_text = "[橙] 残血高危 (精力枯竭且持续承压)"
                    else:
                        alert_text = "[橙] 疲劳积压 (长时间高压未获缓冲)"
                else:
                    alert_text = "[黄] 慢性高压 (处于警戒区时间过长)"

            # ==========================================
            # 5. 静默期拦截机制 与 报警输出
            # ==========================================
            if target_tier > current_alert_tier:
                # 判定是否拦截
                intercepted = False
                if is_resting_and_recovering:
                    # 休息回血中：如果绝对值没真正破防(< Tier 2)，只是疲劳积分触发报警，予以拦截静默
                    if intensity_tier < 2:
                        intercepted = True
                        
                if not intercepted:
                    alerts.append({
                        "type": alert_text, 
                        "time": time_str, 
                        "S": round(S, 2),
                        "E": round(E, 2),
                        "state": state,
                        "trigger_source": trigger_source,
                        "intensity_zone": intensity_zone,
                        "continuous_hours": round(continuous_hours, 2),
                        "current_events": current_events,
                        "dominant_stressors": dominant_stressors,
                        "C": target_tier / 3.0 
                    })
                    current_alert_tier = target_tier
                
            # 6. 计算置信度 
            ratio = auc_level / self.auc_limit
            C_t = ratio ** 1.8 
            confidence_series.append(C_t)
            
        # ==========================================
        # 7. 日终兜底复查
        # ==========================================
        if results and current_alert_tier < 3:
            last_row = results[-1]
            last_S = last_row.get("S", 0.0)
            last_E = last_row.get("E", DEFAULT_INITIAL_ENERGY)
            last_state = last_row.get("state", "UNKNOWN")
            last_time = last_row.get("time", "00:00")
            last_ch = last_row.get("continuous_hours", 0.0)
            last_ce = last_row.get("current_events", [])
            last_ds = last_row.get("dominant_stressors", [])
            
            # 检查绝对峰值
            if last_S >= self.S_thresh + self.critical_buffer_ratio * self.buffer_zone:
                alerts.append({
                    "type": "[红] 极度高压 (日终防线彻底击穿)", "time": last_time, 
                    "S": round(last_S, 2), "E": round(last_E, 2), "state": last_state,
                    "trigger_source": "intensity_spike", "intensity_zone": "critical",
                    "continuous_hours": round(last_ch, 2), "current_events": last_ce, 
                    "dominant_stressors": last_ds, "C": 1.0
                })
            # 检查疲劳积压情况
            elif auc_level > self.auc_orange and current_alert_tier < 2:
                alerts.append({
                    "type": "[橙] 高危积压 (日终带着严重疲劳入睡)", "time": last_time, 
                    "S": round(last_S, 2), "E": round(last_E, 2), "state": last_state,
                    "trigger_source": "duration_buildup", "intensity_zone": "approaching" if last_S >= self.S_thresh - self.warning_buffer_ratio * self.buffer_zone else "safe",
                    "continuous_hours": round(last_ch, 2), "current_events": last_ce, 
                    "dominant_stressors": last_ds, "C": 0.8
                })
            
        return alerts, confidence_series
