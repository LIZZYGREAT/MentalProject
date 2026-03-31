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
        self.predictor = MarkovRegimePredictor(seed=seed)

    def update_user(self, user: User) -> None:
        """接收前端动态修改的参数，刷新引擎配置"""
        self.user = user
        self.time_step = self.user.get_param("time_step", self.time_step)

    def simulate_day(self, events: List[BaseEvent], prev_S_end: Optional[float] = None, 
                     prev_E_end: Optional[float] = None, date_str: str = None):
        
        self.user._init_strategies()
        S_star = self.user.get_current_S_star()
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
            
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
        state_machine = PhysiologyStateMachine(schedule)
        
        base_date = datetime.strptime(date_str, "%Y-%m-%d")
        current_time = base_date
        end_of_day = base_date + timedelta(days=1) - timedelta(minutes=1) 

        results, trace_logs, event_profile = [], [], {}
        continuous_load_hours, sleep_elapsed_minutes = 0.0, 0.0
        last_load_end_time = base_date
        rest_session = RestSession()
        
        wake_s, wake_recorded = S, False
        is_penalizing, energy_exhausted = False, False

        # === 突变平滑控制流水线 ===
        friction_excess_stress = 0.0  
        dopamine_buffer = 0.0         
        momentum_S_1 = 0.0            
        momentum_S_2 = 0.0            
        beta_momentum = 0.40           

        # [事件驱动马尔可夫]：追踪触发器的局部变量
        prev_state_name = None
        prev_active_event_ids = set()
        last_jump_fatigue = 0.0
        minutes_since_last_jump = 0
        last_actual_step_S = 0.0
        
        # --- [修复不可复现Bug：强制重置摇号机状态] ---
        # 结合日期哈希，确保在相同参数和同一天，摇出的随机序列永远100%一致
        date_hash = sum(ord(c) for c in date_str)
        rng_seed_val = int(self.user.get_param("random_seed", 42))
        
        # 1. 强行重置马尔可夫预测器的核心 RNG
        self.predictor.rng = np.random.RandomState((rng_seed_val + date_hash + 999) % (2**32 - 1))
        
        # 2. 强行重置本局的冷却游走 RNG
        local_rng = np.random.RandomState((rng_seed_val + date_hash) % (2**32 - 1))
        next_wandering_cooldown = 40 + local_rng.randint(-10, 16)
        # ----------------------------------------------

        # 循环不变量外提
        static_f_strategy = self.user.get_f_strategy()
        static_c_strategy = self.user.get_C_strategy()
        static_rest_strategy = self.user.get_rest_strategy()
        static_night_strategy = self.user.get_night_strategy()
        static_resilience = self.user.get_resilience_index()

        trace_logs.append(f"[00:00] 🌟 核心引擎仿真启动 (基线 S*={S_star:.1f}, 初始S={S:.1f}, 初始E={E:.1f})")

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
            
            # === [马尔可夫情绪区制跳跃 (事件驱动)] ===
            current_active_ids = {ev.event_id for ev in active_high_loads} if has_high_load else ({routine_ev.event_id} if routine_ev else set())
            
            trigger_reason = None
            if state not in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
                if prev_state_name and state != prev_state_name:
                    trigger_reason = "状态切变"
                elif prev_active_event_ids != current_active_ids:
                    trigger_reason = "任务切变"
                elif continuous_load_hours - last_jump_fatigue >= 1.0:
                    trigger_reason = "疲劳关口"
                elif last_actual_step_S > 2.0:
                    trigger_reason = "应激刺穿"
                elif minutes_since_last_jump >= next_wandering_cooldown:
                    trigger_reason = "认知游走"

            # 如果满足触发条件，或是每天第一步
            if trigger_reason or current_time == base_date:
                minutes_since_last_jump = 0
                next_wandering_cooldown = 40 + int(self.predictor.rng.uniform(-10, 15))
                last_jump_fatigue = continuous_load_hours
                
                # 提取环境强度与类型
                intensity = 0.0
                event_type = "rest"
                if has_high_load:
                    weights = [ev.get_fatigue_weight() for ev in active_high_loads if hasattr(ev, 'get_fatigue_weight')]
                    intensity = max(weights) if weights else 0.85
                    event_type = active_high_loads[0].get_event_type()
                elif routine_ev:
                    event_type = routine_ev.get_event_type()

                user_features = {
                    "fatigue": continuous_load_hours, 
                    "debt": self.user.get_sleep_debt(), 
                    "stress_gap": max(0.0, (S - S_star) / 50.0),
                    "resilience": static_resilience,    
                    "current_state": state,
                    "has_high_load": has_high_load,
                    "f_strategy": static_f_strategy,    
                    "c_strategy": static_c_strategy,    
                    "rest_strategy": static_rest_strategy, 
                    "night_strategy": static_night_strategy,
                    "intensity": intensity,
                    "event_type": event_type
                }
                
                old_regime = self.predictor.current_regime
                new_regime, probs = self.predictor.predict_next_regime(user_features)
                
                # 仅在区制真的发生改变时输出日志，保持纯净
                if new_regime != old_regime:
                    emoji = "🔥" if new_regime == "FLOW" else ("🌪️" if new_regime == "FRICTION" else "🧘")
                    reason_str = trigger_reason if trigger_reason else "引擎启动"
                    trace_logs.append(f"[{cur_str}] {emoji} [{reason_str}] 区制切换: {old_regime} -> {new_regime} "
                                      f"(心流:{probs['FLOW']*100:.1f}%, 内耗:{probs['FRICTION']*100:.1f}%, 挑战度={intensity:.2f})")
                    
                    # 触发顿悟脉冲
                    if old_regime == "FRICTION" and new_regime == "FLOW":
                        refund = min(friction_excess_stress * 0.8, 8.0) 
                        base_bonus = 2.0
                        res_bonus = max(0.0, static_resilience) * 1.5
                        
                        total_dopamine = base_bonus + refund + res_bonus
                        dopamine_buffer += total_dopamine
                        friction_excess_stress = 0.0 
                        
                        trace_logs.append(f"[{cur_str}] 💡 [认知破局] 顿悟触发！释放 {total_dopamine:.1f} 点内啡肽入池 (含退还积分 {refund:.1f})")
            else:
                minutes_since_last_jump += self.time_step

            # 同步历史指针
            prev_state_name = state
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

            # [核心计算微观增量]
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
            
            # [挂载马尔可夫情绪区制控制乘数]
            delta_S, delta_E = self.predictor.apply_regime_modifiers(delta_S, delta_E)

            # === 🧩 1. 积分退还累计 ===
            if self.predictor.current_regime == "FRICTION" and (has_high_load or routine_ev):
                friction_excess_stress += max(0.0, delta_S - base_ds)
            elif self.predictor.current_regime != "FRICTION":
                friction_excess_stress = max(0.0, friction_excess_stress - 0.2 * (self.time_step / 5.0))

            # === 🧩 2. 多巴胺缓冲池释放 (漏桶算法) ===
            if dopamine_buffer > 0:
                # [修复凸点Bug]：将单步最大释放量 2.5 暴降至 0.15。
                # 让多巴胺可以在近 1-2 个小时内平滑发挥降压效果，消除尖刺
                release = min(dopamine_buffer, 0.15 * (self.time_step / 5.0)) 
                dopamine_buffer -= release
                delta_S -= release         
                delta_E += release * 0.4   
                if dopamine_buffer <= 0.01:
                    trace_logs.append(f"[{cur_str}] 📉 [缓冲池耗尽] 顿悟多巴胺代谢完毕，曲线恢复平滑爬坡")

            delta_S, delta_E = self._apply_epoc_absorption(state, has_high_load, delta_S, delta_E, trace_logs, cur_str, static_resilience)
            delta_S += inertia_ds
            delta_E += inertia_de

            # === 🧩 3. 动量平滑与生理放行 ===
            is_physiological_rest = state in ["RECOVERY_SLEEP", "NIGHT_SLEEP", "ROUTINE_MAINTENANCE"] or (state == "DAY_ACTIVE" and not has_high_load)
            
            if not is_physiological_rest:
                momentum_S_1 = beta_momentum * momentum_S_1 + (1.0 - beta_momentum) * delta_S
                momentum_S_2 = beta_momentum * momentum_S_2 + (1.0 - beta_momentum) * momentum_S_1
                actual_step_S = momentum_S_2  
            else:
                momentum_S_1 = delta_S
                momentum_S_2 = delta_S
                actual_step_S = delta_S

            last_actual_step_S = actual_step_S

            self._update_profiles(active_high_loads if has_high_load else ([routine_ev] if routine_ev else []), event_profile, delta_S, delta_E, f_pen)

            if f_pen > 0 and not is_penalizing:
                trace_logs.append(f"[{cur_str}] ⚠️ 触发连轴转惩罚 (折算负荷达 {continuous_load_hours:.1f}h)")
                is_penalizing = True
            elif f_pen == 0 and is_penalizing:
                trace_logs.append(f"[{cur_str}] ♻️ 连续惩罚解除，压力累积放缓")
                is_penalizing = False

            if E < 20.0 and not energy_exhausted:
                trace_logs.append(f"[{cur_str}] 🪫 精力耗竭 (E < 20)，认知防御受损！")
                energy_exhausted = True
            elif E >= 20.0 and energy_exhausted:
                energy_exhausted = False

            S = max(0.0, min(150.0, S + actual_step_S))  
            E = max(0.0, min(100.0, E + delta_E))
            
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
        if is_resting and epoc_level > 0:
            consume = min(epoc_level, 1.5 * (self.time_step / 5.0))
            self.user.epoc_level -= consume
            delta_E += consume * (0.6 + 0.2 * res_idx)
            delta_S += -consume * (0.08 + 0.05 * res_idx)
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
            overlap_modifier = 1.0 + 0.3 * math.log(load_count)
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