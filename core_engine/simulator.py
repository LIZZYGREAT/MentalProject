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
    """跟踪离开高负荷后的连续休息时长，供连续负荷惩罚恢复率使用。"""
    def __init__(self):
        self.active, self.duration, self.total_accumulated = False, 0.0, 0.0
    def tick(self, time_step: int):
        """本步处于空闲恢复时累加 duration。"""
        self.active, self.duration, self.total_accumulated = True, self.duration + time_step, self.total_accumulated + time_step
    def reset(self):
        """重新进入高负荷时清空当前段。"""
        self.active, self.duration = False, 0.0
    def get_duration(self) -> float:
        """返回当前休息段累计分钟数。"""
        return self.duration

class Simulator:
    """按 time_step 分钟推进一日，RK4 积分 S/E，叠加热区制、微观缓冲池与事件画像聚合。"""
    def __init__(self, user: User, time_step: int = 5):
        """
        参数 user: 提供参数与策略；time_step: 积分步长（分钟），缺省读 user.params['time_step']。
        """
        self.user = user
        self.time_step = time_step or self.user.get_param("time_step", 5)
        seed = int(self.user.get_param("random_seed", 42))
        self.predictor = MarkovRegimePredictor(seed=seed, params=self.user.params)

    def update_user(self, user: User) -> None:
        """替换仿真对象并同步步长与马尔可夫参数表。"""
        self.user = user
        self.time_step = self.user.get_param("time_step", self.time_step)
        self.predictor.params = self.user.params

    def _evaluate_derivatives(self, S_temp: float, E_temp: float, current_time: datetime, 
                            active_events: list, routine_ev, state: str, 
                            elapsed_mins: float, continuous_hours: float, 
                            sleep_eff: float, micro_cfg: dict, 
                            step_noise_s: float = 0.0, step_noise_e: float = 0.0,
                            is_substep: bool = False) -> Tuple[float, float, float, float, list]:
        """
        RK4 子步用的瞬时导数。step_noise_s/e 参数以实现步长锁定噪声。
        """
        delta_S = 0.0
        delta_E = 0.0
        f_pen = 0.0
        components = []
        
        # 1. 基础事件冲击计算
        if active_events:
            for ev in active_events:
                # 透传步长锁定噪声
                ds, de = ev.calculate_stress_impact_dual(
                    self.user, S_temp, E_temp, current_time, self.time_step, 
                    step_noise_s=step_noise_s, step_noise_e=step_noise_e, is_substep=is_substep
                )
                delta_S += ds
                delta_E += de
                components.append((ev.event_id, ds, de))
                
            load_count = len(active_events)
            if load_count > 1:
                log_base = micro_cfg.get("concurrent_log_base", 0.3)
                overlap_modifier = 1.0 + log_base * math.log(load_count)
                delta_S = (delta_S / load_count) * overlap_modifier
                delta_E = (delta_E / load_count) * overlap_modifier
                
            f_pen = self.user.course_strategy.calculate_fatigue(continuous_hours)
            delta_S += f_pen
            
        elif routine_ev:
            # 透传步长锁定噪声给 Meal/Nap/Sleep 等事件
            ds, de = routine_ev.calculate_stress_impact_dual(
                self.user, S_temp, E_temp, current_time, self.time_step, 
                step_noise_s=step_noise_s, step_noise_e=step_noise_e, is_substep=is_substep
            )
                
            if routine_ev.get_event_type() == "sleep":
                ds *= sleep_eff if ds < 0 else 1.0
                de *= sleep_eff if de > 0 else 1.0
            delta_S = ds
            delta_E = de
            components.append((routine_ev.event_id, ds, de))
            
        else:
            if state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
                # 睡眠策略锁定噪声应用
                ds, de = self.user.night_strategy.calculate_step(
                    S_temp, E_temp, current_time, self.time_step, elapsed_mins, 
                    step_noise_s=step_noise_s, step_noise_e=step_noise_e, is_substep=is_substep
                )
                delta_S = ds * sleep_eff if ds < 0 else ds
                delta_E = de * sleep_eff if de > 0 else de
                components.append(("night_sleep", ds, de))
            elif state == "LATE_NIGHT_ACTIVE":
                # 深夜主动状态：固定轻微负荷，受步长噪声扰动
                delta_S = 0.08 + step_noise_s * 0.02
                delta_E = -0.12 + step_noise_e * 0.02
            else:
                # 空闲休息锁定噪声应用
                ds, de = self.user.rest_strategy.calculate_flow_recovery(
                    S_temp, E_temp, elapsed_mins, self.time_step, self.user.get_current_S_star(), 
                    step_noise_s=step_noise_s, step_noise_e=step_noise_e, is_substep=is_substep
                )
                delta_S, delta_E = ds, de
                components.append(("rest", ds, de))
                
        base_ds = delta_S
        
        # 2. 挂载动态马尔可夫区制乘数
        intensity = 0.0
        if active_events:
            intensity = max([getattr(ev, 'intensity', 0.85) for ev in active_events] + [0.0])
            
        modifier_features = {
            "f_strategy": self.user.get_f_strategy(),
            "intensity": intensity,
            "resilience": self.user.get_resilience_index(),
            "fatigue": continuous_hours
        }
        
        # 马尔可夫拦截器：根据区制对导数进行缩放
        final_ds, final_de = self.predictor.apply_regime_modifiers(delta_S, delta_E, modifier_features, is_substep=is_substep)
        
        return final_ds, final_de, base_ds, f_pen, components

    def simulate_day(self, events: List[BaseEvent], prev_S_end: Optional[float] = None, 
                     prev_E_end: Optional[float] = None, date_str: str = None):
        """核心仿真主循环：RK4 积分结合离散状态切换。"""
        self.user._init_strategies()
        S_star = self.user.get_current_S_star()
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        
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

        # 微观化学离散状态池
        energy_buffer = 0.0           
        friction_excess_stress = 0.0  
        dopamine_buffer = 0.0         
        momentum_S_1 = 0.0            
        momentum_S_2 = 0.0            
        beta_momentum = micro_cfg.get("momentum_beta", 0.40)
        exhaustion_th = micro_cfg.get("energy_exhaustion_threshold", 20.0)

        prev_active_event_ids = set()
        minutes_since_last_macro_check = 0.0
        
        # 统一由 Predictor 的 RNG 负责生成所有随机源，确保可复现性
        date_hash = sum(ord(c) for c in date_str)
        rng_seed_val = int(self.user.get_param("random_seed", 42))
        self.predictor.rng = np.random.RandomState((rng_seed_val + date_hash + 999) % (2**32 - 1))

        static_f_strategy = self.user.get_f_strategy()
        static_resilience = self.user.get_resilience_index()

        trace_logs.append(f"[{current_time.strftime('%H:%M')}] 混合时钟引擎启动 (积分步长={self.time_step}m, S_star={S_star:.1f})")

        while current_time <= end_of_day:
            cur_str = current_time.strftime("%H:%M")
            
            # --- 步长锁定噪声采样 ---
            # 每个步长仅采样一次，贯穿 RK4 子步
            current_step_noise_s = self.predictor.rng.normal(0, 1.0)
            current_step_noise_e = self.predictor.rng.normal(0, 1.0)
            
            if not wake_recorded and current_time >= schedule["wake_time"]:
                wake_s, wake_recorded = S, True
                trace_logs.append(f"[{cur_str}] 截取清晨唤醒状态 Wake_S = {wake_s:.1f}")

            active_high_loads = timeline.get_active_high_load_events(current_time)
            routine_ev = timeline.get_active_routine(current_time)
            has_high_load = len(active_high_loads) > 0

            state, inertia_ds, inertia_de, sm_logs = state_machine.determine_state(current_time, has_high_load, routine_ev)
            trace_logs.extend(sm_logs)
            
            intensity = 0.0
            event_type = "rest"
            if has_high_load:
                weights = [ev.get_fatigue_weight() for ev in active_high_loads if hasattr(ev, 'get_fatigue_weight')]
                intensity = max(weights) if weights else 0.85
                event_type = active_high_loads[0].get_event_type()
            elif routine_ev:
                event_type = routine_ev.get_event_type()

            current_active_ids = {ev.event_id for ev in active_high_loads} if has_high_load else ({routine_ev.event_id} if routine_ev else set())

            # 马尔可夫预测触发判定
            trigger_reason = None
            if state not in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
                if prev_active_event_ids != current_active_ids:
                    trigger_reason = "任务切变中断"
                elif E < exhaustion_th and not energy_exhausted:
                    trigger_reason = "耗竭红线中断"
                elif minutes_since_last_macro_check >= macro_interval:
                    trigger_reason = f"宏观巡检({macro_interval}m)"

            if trigger_reason or current_time == base_date:
                user_features = {
                    "fatigue": continuous_load_hours, 
                    "debt": self.user.get_sleep_debt(), 
                    "resilience": static_resilience,    
                    "f_strategy": static_resilience,    
                    "intensity": intensity,
                    "event_type": event_type
                }
                user_features["f_strategy"] = static_f_strategy

                old_regime = self.predictor.current_regime
                new_regime, probs_log = self.predictor.predict_next_regime(user_features, minutes_since_last_macro_check)
                minutes_since_last_macro_check = 0.0
                
                if new_regime != old_regime:
                    phi_val = probs_log.get('Phi', 0.0)
                    p_jump_val = probs_log.get('P_jump', 0.0) * 100
                    trace_logs.append(f"[{cur_str}] [{new_regime}] 区制跳跃: {old_regime} -> {new_regime} (Φ={phi_val:.2f}, 风险={p_jump_val:.1f}%)")
                    
                    if old_regime == "FRICTION" and new_regime == "FLOW":
                        refund_ratio = micro_cfg.get("epiphany_refund_ratio", 0.8)
                        refund = min(friction_excess_stress * refund_ratio, 8.0) 
                        total_dopamine = micro_cfg.get("epiphany_base_bonus", 2.0) + refund + max(0.0, static_resilience) * 1.5
                        dopamine_buffer += total_dopamine
                        friction_excess_stress = 0.0 
                        trace_logs.append(f"[{cur_str}] [认知破局] 顿悟触发，释放 {total_dopamine:.1f} 入池")
            else:
                minutes_since_last_macro_check += self.time_step

            prev_active_event_ids = current_active_ids

            # 更新连续负荷与休息会话
            if has_high_load:
                continuous_load_hours += (self.time_step / 60.0) * max([ev.get_fatigue_weight() for ev in active_high_loads if hasattr(ev, 'get_fatigue_weight')] + [0.85])
                last_load_end_time = current_time + timedelta(minutes=self.time_step)
                rest_session.reset()
                self._init_profiles(active_high_loads, event_profile)
            else:
                if state == "DAY_ACTIVE":
                    if (current_time - last_load_end_time).total_seconds() / 60.0 >= 5.0:
                        continuous_load_hours = max(0.0, continuous_load_hours - (self.time_step / 60.0) * self.user.course_strategy.get_penalty_recovery_rate())
                    rest_session.tick(self.time_step)
                
                if routine_ev:
                    self._init_profiles([routine_ev], event_profile)

            if state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"] or (routine_ev and routine_ev.get_event_type() == "sleep"):
                sleep_elapsed_minutes += self.time_step
            else:
                sleep_elapsed_minutes = 0.0

            # --- RK4 积分，全子步共享相同的锁定噪声 ---
            k1_S, k1_E, base_ds_k1, f_pen_k1, k1_comp = self._evaluate_derivatives(
                S, E, current_time, active_high_loads, routine_ev, state, 
                sleep_elapsed_minutes, continuous_load_hours, state_machine.sleep_eff, 
                micro_cfg, step_noise_s=current_step_noise_s, step_noise_e=current_step_noise_e, is_substep=False
            )
            
            mid_time = current_time + timedelta(minutes=self.time_step/2.0)
            k2_S, k2_E, _, _, _ = self._evaluate_derivatives(
                S + k1_S/2, E + k1_E/2, mid_time, active_high_loads, routine_ev, state, 
                sleep_elapsed_minutes + self.time_step/2.0, continuous_load_hours, 
                state_machine.sleep_eff, micro_cfg, step_noise_s=current_step_noise_s, step_noise_e=current_step_noise_e, is_substep=True
            )
            
            k3_S, k3_E, _, _, _ = self._evaluate_derivatives(
                S + k2_S/2, E + k2_E/2, mid_time, active_high_loads, routine_ev, state, 
                sleep_elapsed_minutes + self.time_step/2.0, continuous_load_hours, 
                state_machine.sleep_eff, micro_cfg, step_noise_s=current_step_noise_s, step_noise_e=current_step_noise_e, is_substep=True
            )
            
            end_time = current_time + timedelta(minutes=self.time_step)
            k4_S, k4_E, _, _, _ = self._evaluate_derivatives(
                S + k3_S, E + k3_E, end_time, active_high_loads, routine_ev, state, 
                sleep_elapsed_minutes + self.time_step, continuous_load_hours, 
                state_machine.sleep_eff, micro_cfg, step_noise_s=current_step_noise_s, step_noise_e=current_step_noise_e, is_substep=True
            )
            
            delta_S = (k1_S + 2*k2_S + 2*k3_S + k4_S) / 6.0
            delta_E = (k1_E + 2*k2_E + 2*k3_E + k4_E) / 6.0

            # 离散微观池处理
            if routine_ev and routine_ev.get_event_type() in ["meal", "nap"] and delta_E > 0:
                energy_buffer += delta_E
                delta_E = 0.0 

            if self.predictor.current_regime == "FRICTION":
                friction_excess_stress += max(0.0, delta_S - base_ds_k1)
            else:
                friction_excess_stress = max(0.0, friction_excess_stress - 0.2 * (self.time_step / 5.0))

            if dopamine_buffer > 0:
                release = min(dopamine_buffer, 0.15 * (self.time_step / 5.0)) 
                dopamine_buffer -= release
                delta_S -= release         
                delta_E += release * 0.4   
                if dopamine_buffer <= 0.01: trace_logs.append(f"[{cur_str}] 多巴胺代谢完毕。")

            delta_S, delta_E = self._apply_epoc_absorption(state, has_high_load, delta_S, delta_E, trace_logs, cur_str, static_resilience)
            delta_S += inertia_ds
            delta_E += inertia_de

            # AR(1) 平滑滤波器
            is_physiological_rest = state in ["RECOVERY_SLEEP", "NIGHT_SLEEP", "ROUTINE_MAINTENANCE"] or (state == "DAY_ACTIVE" and not has_high_load)
            if not is_physiological_rest:
                momentum_S_1 = beta_momentum * momentum_S_1 + (1.0 - beta_momentum) * delta_S
                momentum_S_2 = beta_momentum * momentum_S_2 + (1.0 - beta_momentum) * momentum_S_1
                final_S_step = momentum_S_2  
                momentum_trace = (
                    f"$$ AR1(Filter): S'_{{m1}} = {beta_momentum:.2f}S_{{m1}} + {1.0-beta_momentum:.2f}\\Delta S_{{in}} = {momentum_S_1:.3f} $$<br>"
                    f"$$ S'_{{m2}} = {beta_momentum:.2f}S_{{m2}} + {1.0-beta_momentum:.2f}S'_{{m1}} = {final_S_step:.3f} $$"
                )
            else:
                momentum_S_1 = momentum_S_2 = final_S_step = delta_S
                momentum_trace = ""

            # 精力池与高度耦合的基础代谢更新
            decay_rate = micro_cfg.get("buffer_decay_rate", 0.05)
            buffer_release = energy_buffer * (1.0 - math.exp(-decay_rate * self.time_step))
            energy_buffer -= buffer_release
            
            if state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"]:
                basal_drain = 0.0
            else:
                base_basal = micro_cfg.get("basal_drain_rate", 0.05) # 基础代谢大幅下调
                stress_gap = max(0.0, S - S_star)
                basal_drain = base_basal * (1.0 + 0.02 * stress_gap) * (self.time_step / 5.0)
                
            raw_step_E = delta_E + buffer_release - basal_drain
            if raw_step_E < 0:
                psi_E = 1.0 / (1.0 + math.exp(-(E - 15.0)))
                raw_step_E *= psi_E
            final_E_step = raw_step_E

            self._update_profiles(active_high_loads if has_high_load else ([routine_ev] if routine_ev else []), event_profile, final_S_step, final_E_step, f_pen_k1, momentum_trace)

            if f_pen_k1 > 0 and not is_penalizing:
                trace_logs.append(f"[{cur_str}] 触发连轴转惩罚 ({continuous_load_hours:.1f}h)")
                is_penalizing = True
            elif f_pen_k1 == 0 and is_penalizing:
                is_penalizing = False

            if E < exhaustion_th and not energy_exhausted:
                trace_logs.append(f"[{cur_str}] 精力耗竭，触发休克阻尼")
                energy_exhausted = True
            elif E >= exhaustion_th and energy_exhausted:
                energy_exhausted = False

            S = max(0.0, min(150.0, S + final_S_step))  
            E = max(0.0, min(100.0, E + final_E_step))
            
            dominant_strs = [p["name"] for p in sorted(event_profile.values(), key=lambda x: x["total_S"], reverse=True)[:2] if p["total_S"] > 0]
            curr_names = [ev.name for ev in active_high_loads] if has_high_load else ([routine_ev.name] if routine_ev else [])
            
            results.append({
                "time": cur_str, "S": S, "E": E, "state": state, "f_pen": f_pen_k1, "delta_S": final_S_step,
                "continuous_hours": continuous_load_hours, "current_events": curr_names, "dominant_stressors": dominant_strs
            })
            current_time += timedelta(minutes=self.time_step)

        trace_logs.append(f"[23:59] 仿真结束 (S={S:.1f}, E={E:.1f})")
        alerts, confidence_series = AlertMonitor(self.user.params).analyze(results)
        
        profile_list = [{"name": d["name"], "type": d["type"], "time": d["time"], "detail": d["detail"], "s_impact": round(d["total_S"], 2), "base_s": round(d["base_S"], 2), "penalty_s": round(d["penalty_S"], 2), "e_impact": round(d["total_E"], 2), "weight_factor": d.get("weight_factor", "无"), "credits": d.get("credits", "N/A"), "hours": d.get("hours", "N/A"), "level_str": d.get("level_str", "N/A"), "math_trace": d.get("math_trace", "")} for d in event_profile.values()]

        return results, S, E, schedule["wake_time"], [schedule["late_night_active_end"], schedule["night_sleep_start"]], alerts, confidence_series, trace_logs, profile_list, wake_s

    def _apply_epoc_absorption(self, state, has_high_load, delta_S, delta_E, trace_logs, cur_str, res_idx):
        """后燃 Buff 吸收逻辑。"""
        is_resting = state in ["RECOVERY_SLEEP", "NIGHT_SLEEP"] or (state == "DAY_ACTIVE" and not has_high_load)
        epoc_level = getattr(self.user, 'epoc_level', 0.0)
        if is_resting and epoc_level > 0:
            consume = min(epoc_level, 1.5 * (self.time_step / 5.0))
            self.user.epoc_level -= consume
            delta_E += consume * (0.6 + 0.2 * res_idx)
            delta_S += -consume * (0.08 + 0.05 * res_idx)
            if self.user.epoc_level <= 0.01 and epoc_level > 0.01:
                trace_logs.append(f"[{cur_str}] EPOC 已吸收完毕。")
        return delta_S, delta_E

    def _init_profiles(self, events, profile_dict):
        """初始化画像条目，避免重复创建。"""
        for ev in events:
            if ev.event_id not in profile_dict:
                st = ev.start_time if isinstance(ev.start_time, str) else ev.start_time.strftime("%H:%M")
                et = ev.end_time if isinstance(ev.end_time, str) else ev.end_time.strftime("%H:%M")
                profile_dict[ev.event_id] = {
                    "name": ev.name, "type": ev.get_event_type(), "time": f"{st.split(' ')[-1]}-{et.split(' ')[-1]}",
                    "detail": ev.metadata.get("detail", ""), "total_S": 0.0, "total_E": 0.0, "steps": 0, "base_S": 0.0, "penalty_S": 0.0,
                    "weight_factor": ev.get_fatigue_weight() if hasattr(ev, 'get_fatigue_weight') else "无",
                    "credits": ev.metadata.get("credits", "N/A"), "hours": ev.metadata.get("hours", "N/A"), "level_str": ev.metadata.get("level_str", "N/A"),
                    "math_trace": ""
                }

    def _update_profiles(self, events, profile_dict, delta_S, delta_E, f_pen, momentum_trace: str = ""):
        """多层公式装配工厂：从内向外包缝各级策略透视。"""
        count = len(events)
        if count == 0: return
        for ev in events:
            if ev.event_id not in profile_dict:
                continue 
            
            p = profile_dict[ev.event_id]
            p["total_S"] += delta_S / count
            p["base_S"] += (delta_S - f_pen) / count
            p["penalty_S"] += f_pen / count
            p["total_E"] += delta_E / count
            p["steps"] += 1
            
            if "math_trace" in ev.metadata and not p["math_trace"]:
                base_trace = ev.metadata["math_trace"]
                strat_trace = ""
                if ev.get_event_type() in ["course", "task", "library", "gym"]:
                    if hasattr(self.user, "course_strategy") and hasattr(self.user.course_strategy.f_strategy, "last_trace"):
                        strat_trace = self.user.course_strategy.f_strategy.last_trace
                elif ev.get_event_type() in ["rest", "meal", "nap", "sleep"]:
                    if hasattr(self.user, "rest_strategy") and hasattr(self.user.rest_strategy, "last_trace"):
                        strat_trace = self.user.rest_strategy.last_trace
                        
                pen_trace = ""
                if f_pen > 0 and hasattr(self.user, "course_strategy") and hasattr(self.user.course_strategy, "last_penalty_trace"):
                    pen_trace = self.user.course_strategy.last_penalty_trace
                    
                markov_mod = getattr(self.predictor, 'last_s_mod', 1.0)
                regime = self.predictor.current_regime
                markov_trace = f"$$ Markov({regime}): |\\Delta S| \\xrightarrow{{mod}} |\\Delta S| \\times {markov_mod:.2f} $$"
                
                parts = [t for t in [strat_trace, pen_trace, base_trace, markov_trace, momentum_trace] if t]
                p["math_trace"] = "<hr>".join(parts)