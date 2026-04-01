# core_engine/simulator.py
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import math
import numpy as np

from entity.user import User
from event.base import BaseEvent
from utils.alert_monitor import AlertMonitor  
from core_engine.timeline_manager import TimelineManager
from core_engine.state_machine import PhysiologyStateMachine
from core_engine.markov_predictor import MarkovRegimePredictor

class RestSession:
    def __init__(self):
        self.active, self.duration, self.total_accumulated = False, 0.0, 0.0
    def tick(self, time_step: int):
        self.active, self.duration, self.total_accumulated = True, self.duration + time_step, self.total_accumulated + time_step
    def reset(self):
        self.active, self.duration = False, 0.0
    def get_duration(self) -> float: return self.duration

class Simulator:
    def __init__(self, user: User, time_step: int = 5):
        self.user = user
        self.time_step = time_step or self.user.get_param("time_step", 5)
        seed = int(self.user.get_param("random_seed", 42))
        # 挂载了全新的马尔可夫预测器，并注入用户全局参数配置字典
        self.predictor = MarkovRegimePredictor(seed=seed, params=self.user.params)

    def update_user(self, user: User) -> None:
        """接收前端动态修改的参数，刷新引擎配置"""
        self.user = user
        self.time_step = self.user.get_param("time_step", self.time_step)
        # 同步刷新马尔可夫预测器的参数引用
        self.predictor.params = self.user.params

    def simulate_day(self, events: List[BaseEvent], prev_S_end: Optional[float] = None, 
                     prev_E_end: Optional[float] = None, date_str: str = None):
        
        self.user._init_strategies()
        S_star = self.user.get_current_S_star()
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        
        # 动态拉取引擎微观参数与宏观马尔可夫参数
        micro_cfg = self.user.get_param("simulator_micro_params", {})
        macro_cfg = self.user.get_param("markov_semi_params", {})
        macro_interval = macro_cfg.get("regime_check_interval", 25)
            
        if prev_S_end is None:
            seed_val = int(self.user.get_param("random_seed", 42))
            rng_start = np.random.RandomState((seed_val + sum(ord(c) for c in date_str)) % (2**32 - 1))
            S = S_star + abs(rng_start.normal(0.0, S_star * 0.2))
        else:
            S = prev_S_end

        E = prev_E_end if prev_E_end is not None else 100.0

        # === 1. 初始化引擎组件 ===
        timeline = TimelineManager(events, date_str)
        schedule = timeline.analyze_schedule()
        state_machine = PhysiologyStateMachine(schedule, params=self.user.params)
        
        base_date = datetime.strptime(date_str, "%Y-%m-%d")
        current_time = base_date
        end_of_day = base_date + timedelta(days=1) - timedelta(minutes=1) 

        results, trace_logs, event_profile = [], [], {}
        continuous_load_hours, sleep_elapsed_minutes = 0.0, 0.0
        last_load_end_time = base_date
        rest_session = RestSession()
        
        wake_s, wake_recorded = S, False
        is_penalizing, energy_exhausted = False, False

        # === 大一统动力学与突变平滑控制流水线 ===
        energy_buffer = 0.0           # [新增] 房室消化/吸收缓冲池
        friction_excess_stress = 0.0  
        dopamine_buffer = 0.0         
        momentum_S_1 = 0.0            
        momentum_S_2 = 0.0            
        beta_momentum = micro_cfg.get("momentum_beta", 0.40)
        exhaustion_th = micro_cfg.get("energy_exhaustion_threshold", 20.0)

        # [半马尔可夫双轨时钟控制]：追踪宏观决策时钟与异步中断状态
        prev_active_event_ids = set()
        minutes_since_last_macro_check = 0.0
        
        # 强行重置马尔可夫预测器的核心 RNG (保证纯函数性质)
        date_hash = sum(ord(c) for c in date_str)
        rng_seed_val = int(self.user.get_param("random_seed", 42))
        self.predictor.rng = np.random.RandomState((rng_seed_val + date_hash + 999) % (2**32 - 1))

        # 循环不变量外提
        static_f_strategy = self.user.get_f_strategy()
        static_resilience = self.user.get_resilience_index()

        trace_logs.append(f"[00:00] 🌟 混合时钟引擎启动 (积分步长={self.time_step}m, 巡检步长={macro_interval}m, S_star={S_star:.1f})")

        # === 2. 主循环 ===
        while current_time <= end_of_day:
            cur_str = current_time.strftime("%H:%M")
            
            # [唤醒锚点记录]
            if not wake_recorded and current_time >= schedule["wake_time"]:
                wake_s, wake_recorded = S, True
                trace_logs.append(f"[{cur_str}] 🌅 截取清晨唤醒状态 Wake_S = {wake_s:.1f} (用作双轨演化锚点)")

            # [查询当前活跃事件流]
            active_high_loads = timeline.get_active_high_load_events(current_time)
            routine_ev = timeline.get_active_routine(current_time)
            has_high_load = len(active_high_loads) > 0

            # [状态机流转判定]
            state, inertia_ds, inertia_de, sm_logs = state_machine.determine_state(current_time, has_high_load, routine_ev)
            trace_logs.extend(sm_logs)
            
            # 提取环境强度与类型
            intensity = 0.0
            event_type = "rest"
            if has_high_load:
                weights = [ev.get_fatigue_weight() for ev in active_high_loads if hasattr(ev, 'get_fatigue_weight')]
                intensity = max(weights) if weights else 0.85
                event_type = active_high_loads[0].get_event_type()
            elif routine_ev:
                event_type = routine_ev.get_event_type()

            current_active_ids = {ev.event_id for ev in active_high_loads} if has_high_load else ({routine_ev.event_id} if routine_ev else set())

            # === [异步事件中断判定] ===
            trigger_reason = None
            # 生理重度睡眠期间，冻结区制跳跃
            if state not in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
                if prev_active_event_ids != current_active_ids:
                    trigger_reason = "任务切变中断"
                elif E < exhaustion_th and not energy_exhausted:
                    trigger_reason = "耗竭红线中断"
                elif minutes_since_last_macro_check >= macro_interval:
                    trigger_reason = f"宏观巡检({macro_interval}m)"

            # === 🧩 [马尔可夫情绪区制预测 (仅在宏观检查点或中断时拉起)] ===
            if trigger_reason or current_time == base_date:
                # 构建特征工程字典，传入势能计算器
                user_features = {
                    "fatigue": continuous_load_hours, 
                    "debt": self.user.get_sleep_debt(), 
                    "resilience": static_resilience,    
                    "f_strategy": static_f_strategy,    
                    "intensity": intensity,
                    "event_type": event_type
                }
                
                old_regime = self.predictor.current_regime
                # 调用半马尔可夫跃迁核
                new_regime, probs_log = self.predictor.predict_next_regime(user_features, minutes_since_last_macro_check)
                
                # 清零宏观计步器
                minutes_since_last_macro_check = 0.0
                
                if new_regime != old_regime:
                    emoji = "🔥" if new_regime == "FLOW" else ("🌪️" if new_regime == "FRICTION" else "🧘")
                    reason_str = trigger_reason if trigger_reason else "引擎启动"
                    phi_val = probs_log.get('Phi', 0.0)
                    p_jump_val = probs_log.get('P_jump', 0.0) * 100
                    
                    trace_logs.append(f"[{cur_str}] {emoji} [{reason_str}] 区制跳跃: {old_regime} -> {new_regime} "
                                      f"(承压势能Φ={phi_val:.2f}, 破裂风险={p_jump_val:.1f}%)")
                    
                    # 触发顿悟脉冲 (从内耗杀回心流的量子隧穿奖励)
                    if old_regime == "FRICTION" and new_regime == "FLOW":
                        refund_ratio = micro_cfg.get("epiphany_refund_ratio", 0.8)
                        refund_max = micro_cfg.get("epiphany_refund_max", 8.0)
                        refund = min(friction_excess_stress * refund_ratio, refund_max) 
                        
                        base_bonus = micro_cfg.get("epiphany_base_bonus", 2.0)
                        res_k = micro_cfg.get("epiphany_resilience_k", 1.5)
                        res_bonus = max(0.0, static_resilience) * res_k
                        
                        total_dopamine = base_bonus + refund + res_bonus
                        dopamine_buffer += total_dopamine
                        friction_excess_stress = 0.0 
                        
                        trace_logs.append(f"[{cur_str}] 💡 [认知破局] 顿悟触发！释放 {total_dopamine:.1f} 点内啡肽入池 (含退还积分 {refund:.1f})")
            else:
                # 累加时间，推演离散风险函数的驻留时间 (d)
                minutes_since_last_macro_check += self.time_step

            # 同步历史环境指针
            prev_active_event_ids = current_active_ids

            # [维护连轴转时长]
            if has_high_load:
                continuous_load_hours += (self.time_step / 60.0) * max([ev.get_fatigue_weight() for ev in active_high_loads if hasattr(ev, 'get_fatigue_weight')] + [0.85])
                last_load_end_time = current_time + timedelta(minutes=self.time_step)
                rest_session.reset()
                self._init_profiles(active_high_loads, event_profile)
            elif state == "DAY_ACTIVE":
                if (current_time - last_load_end_time).total_seconds() / 60.0 >= 5.0:
                    continuous_load_hours = max(0.0, continuous_load_hours - (self.time_step / 60.0) * self.user.course_strategy.get_penalty_recovery_rate())
                rest_session.tick(self.time_step)
            elif state in ["RECOVERY_SLEEP", "NIGHT_SLEEP", "ROUTINE_MAINTENANCE"]:
                continuous_load_hours, rest_session.active, rest_session.duration = 0.0, False, 0.0

            # === 3. 核心计算微观增量 (Micro-Dynamics) ===
            delta_S, delta_E, f_pen = 0.0, 0.0, 0.0
            
            if has_high_load:
                sleep_elapsed_minutes = 0.0
                delta_S, delta_E, f_pen = self._calc_high_load_impact(active_high_loads, S, E, current_time, continuous_load_hours)
            elif routine_ev:
                self._init_profiles([routine_ev], event_profile)
                ds, de = routine_ev.calculate_stress_impact_dual(self.user, S, E, current_time, self.time_step)
                if routine_ev.get_event_type() == "sleep":
                    ds *= state_machine.sleep_eff if ds < 0 else 1.0
                    de *= state_machine.sleep_eff if de > 0 else 1.0
                    sleep_elapsed_minutes += self.time_step
                else: sleep_elapsed_minutes = 0.0
                delta_S, delta_E = ds, de
            else:
                if state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
                    sleep_elapsed_minutes += self.time_step
                    ds, de = self.user.night_strategy.calculate_step(S, E, current_time, self.time_step, sleep_elapsed_minutes)
                    delta_S = ds * state_machine.sleep_eff if ds < 0 else ds
                    delta_E = de * state_machine.sleep_eff if de > 0 else de
                elif state == "LATE_NIGHT_ACTIVE":
                    delta_S, delta_E, sleep_elapsed_minutes = 0.08, -0.12, 0.0
                else:
                    sleep_elapsed_minutes = 0.0
                    delta_S, delta_E = self.user.rest_strategy.calculate_flow_recovery(S, E, rest_session.get_duration(), self.time_step, S_star)

            base_ds = delta_S
            
            # [新增] 房室缓冲池拦截：拦截进食/午休的锐角回血，使其转为指数平滑释放
            if routine_ev and routine_ev.get_event_type() in ["meal", "nap"] and delta_E > 0:
                energy_buffer += delta_E
                delta_E = 0.0 

            # === 🧩 4. 挂载动态区制乘数 (Regime Modifiers) ===
            modifier_features = {
                "f_strategy": static_f_strategy,
                "intensity": intensity,
                "resilience": static_resilience,
                "fatigue": continuous_load_hours
            }
            delta_S, delta_E = self.predictor.apply_regime_modifiers(delta_S, delta_E, modifier_features)

            # === 5. 积分退还累计与多巴胺漏桶释放 ===
            if self.predictor.current_regime == "FRICTION" and (has_high_load or routine_ev):
                friction_excess_stress += max(0.0, delta_S - base_ds)
            elif self.predictor.current_regime != "FRICTION":
                friction_excess_stress = max(0.0, friction_excess_stress - 0.2 * (self.time_step / 5.0))

            if dopamine_buffer > 0:
                leak_rate = micro_cfg.get("dopamine_leak_rate", 0.15)
                release = min(dopamine_buffer, leak_rate * (self.time_step / 5.0)) 
                dopamine_buffer -= release
                delta_S -= release         
                delta_E += release * 0.4   
                if dopamine_buffer <= 0.01:
                    trace_logs.append(f"[{cur_str}] 📉 [缓冲池耗尽] 顿悟多巴胺代谢完毕，曲线恢复平滑爬坡。")

            delta_S, delta_E = self._apply_epoc_absorption(state, has_high_load, delta_S, delta_E, trace_logs, cur_str, static_resilience)
            delta_S += inertia_ds
            delta_E += inertia_de

            # === 6. AR(1) 动量平滑滤波器 ===
            is_physiological_rest = state in ["RECOVERY_SLEEP", "NIGHT_SLEEP", "ROUTINE_MAINTENANCE"] or (state == "DAY_ACTIVE" and not has_high_load)
            
            if not is_physiological_rest:
                momentum_S_1 = beta_momentum * momentum_S_1 + (1.0 - beta_momentum) * delta_S
                momentum_S_2 = beta_momentum * momentum_S_2 + (1.0 - beta_momentum) * momentum_S_1
                actual_step_S = momentum_S_2  
            else:
                momentum_S_1 = delta_S
                momentum_S_2 = delta_S
                actual_step_S = delta_S

            # === 7. 缓冲池释放与基底流失 ===
            # A. 缓冲池指数释放 (平滑血糖吸收)
            decay_rate = micro_cfg.get("buffer_decay_rate", 0.05)
            buffer_release = energy_buffer * (1.0 - math.exp(-decay_rate * self.time_step))
            energy_buffer -= buffer_release
            
            # B. 基础维持生命流失
            if state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
                basal_drain = 0.0
            else:
                basal_drain_base = micro_cfg.get("basal_drain_rate", 0.415)
                basal_drain = basal_drain_base * (self.time_step / 5.0)
                
            # C. 汇总本 Tick 的理论总精力变化
            raw_step_E = delta_E + buffer_release - basal_drain
            
            # === 8.底线生存阻尼 (Lorentzian Floor) ===
            if raw_step_E < 0:
                # 动态读取休克防线阈值
                critical_E = micro_cfg.get("lorentzian_floor_E", 15.0)
                # 当 E 逼近 critical_E 时，大脑强制切断后台代谢，流失率极速衰减至 0
                psi_E = 1.0 / (1.0 + math.exp(-(E - critical_E)))
                raw_step_E *= psi_E
                
            actual_step_E = raw_step_E

            self._update_profiles(active_high_loads if has_high_load else ([routine_ev] if routine_ev else []), event_profile, actual_step_S, actual_step_E, f_pen)

            # [疲劳预警监测]
            if f_pen > 0 and not is_penalizing:
                trace_logs.append(f"[{cur_str}] ⚠️ 触发连轴转惩罚 (折算负荷达 {continuous_load_hours:.1f}h)")
                is_penalizing = True
            elif f_pen == 0 and is_penalizing:
                trace_logs.append(f"[{cur_str}] ♻️ 连续惩罚解除，压力累积放缓")
                is_penalizing = False

            # [异步耗竭红线同步]
            if E < exhaustion_th and not energy_exhausted:
                trace_logs.append(f"[{cur_str}] 🪫 精力耗竭 (E < {exhaustion_th})，神经化学防线受损，触发强制休克阻尼！")
                energy_exhausted = True
            elif E >= exhaustion_th and energy_exhausted:
                energy_exhausted = False

            # [数值安全截断]
            S = max(0.0, min(150.0, S + actual_step_S))  
            E = max(0.0, min(100.0, E + actual_step_E))
            
            dominant_strs = [p["name"] for p in sorted(event_profile.values(), key=lambda x: x["total_S"], reverse=True)[:2] if p["total_S"] > 0]
            curr_names = [ev.name for ev in active_high_loads] if has_high_load else ([routine_ev.name] if routine_ev else [])
            
            results.append({
                "time": cur_str, "S": S, "E": E, "state": state, "f_pen": f_pen, "delta_S": actual_step_S,
                "continuous_hours": continuous_load_hours, "current_events": curr_names, "dominant_stressors": dominant_strs
            })
            current_time += timedelta(minutes=self.time_step)

        trace_logs.append(f"[23:59] 🏁 仿真结束 (终态S={S:.1f}, 终态E={E:.1f})")
        
        alerts, confidence_series = AlertMonitor(self.user.params).analyze(results)
        
        profile_list = [{"name": d["name"], "type": d["type"], "time": d["time"], "detail": d["detail"], "s_impact": round(d["total_S"], 2), "base_s": round(d["base_S"], 2), "penalty_s": round(d["penalty_S"], 2), "e_impact": round(d["total_E"], 2), "weight_factor": d.get("weight_factor", "无"), "credits": d.get("credits", "N/A"), "hours": d.get("hours", "N/A"), "level_str": d.get("level_str", "N/A")} for d in event_profile.values()]

        return results, S, E, schedule["wake_time"], [schedule["late_night_active_end"], schedule["night_sleep_start"]], alerts, confidence_series, trace_logs, profile_list, wake_s

    def _apply_epoc_absorption(self, state, has_high_load, delta_S, delta_E, trace_logs, cur_str, res_idx):
        is_resting = state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"] or (state == "DAY_ACTIVE" and not has_high_load)
        epoc_level = getattr(self.user, 'epoc_level', 0.0)
        
        micro_cfg = self.user.get_param("simulator_micro_params", {})
        
        if is_resting and epoc_level > 0:
            abs_rate = micro_cfg.get("epoc_absorption_rate", 1.5)
            consume = min(epoc_level, abs_rate * (self.time_step / 5.0))
            self.user.epoc_level -= consume
            
            e_base = micro_cfg.get("epoc_abs_e_base", 0.6)
            e_k = micro_cfg.get("epoc_abs_e_k", 0.2)
            delta_E += consume * (e_base + e_k * res_idx)
            
            s_base = micro_cfg.get("epoc_abs_s_base", 0.08)
            s_k = micro_cfg.get("epoc_abs_s_k", 0.05)
            delta_S += -consume * (s_base + s_k * res_idx)
            
            if self.user.epoc_level <= 0.01 and epoc_level > 0.01:
                trace_logs.append(f"[{cur_str}] 🛡️ [后燃结束] 运动或休息积累的放松余温已全部被吸收。")
        return delta_S, delta_E

    def _calc_high_load_impact(self, active_loads, S, E, current_time, continuous_hours) -> Tuple[float, float, float]:
        total_ds_base, total_de = 0.0, 0.0
        for ev in active_loads:
            if hasattr(ev, 'calculate_stress_impact_dual'):
                ds_base, de = ev.calculate_stress_impact_dual(self.user, S, E, current_time, self.time_step)
                total_ds_base += ds_base
                total_de += de
                
        load_count = len(active_loads)
        if load_count > 1:
            micro_cfg = self.user.get_param("simulator_micro_params", {})
            log_base = micro_cfg.get("concurrent_log_base", 0.3)
            overlap_modifier = 1.0 + log_base * math.log(load_count)
            total_ds_base, total_de = (total_ds_base / load_count) * overlap_modifier, (total_de / load_count) * overlap_modifier
            
        f_pen = self.user.course_strategy.calculate_fatigue(continuous_hours)
        return total_ds_base + f_pen, total_de, f_pen

    def _init_profiles(self, events, profile_dict):
        for ev in events:
            if ev.event_id not in profile_dict:
                st = ev.start_time if isinstance(ev.start_time, str) else ev.start_time.strftime("%H:%M")
                et = ev.end_time if isinstance(ev.end_time, str) else ev.end_time.strftime("%H:%M")
                profile_dict[ev.event_id] = {
                    "name": ev.name, "type": ev.get_event_type(), "time": f"{st.split(' ')[-1]}-{et.split(' ')[-1]}",
                    "detail": ev.metadata.get("detail", ""), "total_S": 0.0, "total_E": 0.0, "steps": 0, "base_S": 0.0, "penalty_S": 0.0,
                    "weight_factor": ev.get_fatigue_weight() if hasattr(ev, 'get_fatigue_weight') else "无",
                    "credits": ev.metadata.get("credits", "N/A"), "hours": ev.metadata.get("hours", "N/A"), "level_str": ev.metadata.get("level_str", "N/A")
                }

    def _update_profiles(self, events, profile_dict, delta_S, delta_E, f_pen):
        count = len(events)
        for ev in events:
            p = profile_dict[ev.event_id]
            p["total_S"] += delta_S / count
            p["base_S"] += (delta_S - f_pen) / count
            p["penalty_S"] += f_pen / count
            p["total_E"] += delta_E / count
            p["steps"] += 1