# core_engine/simulator.py
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
import math
import numpy as np

from calibration.trajectory_validation import build_event_trajectory_diagnostics
from entity.user import User
from event.base import BaseEvent
from utils.alert_monitor import AlertMonitor  
from core_engine.timeline_manager import TimelineManager
from core_engine.state_machine import PhysiologyStateMachine
from core_engine.markov_predictor import MarkovRegimePredictor
from algorithm.integration import rk4_step
from algorithm.dynamic_state_model import (
    DynamicInputs,
    LatentState,
    assimilate_observation_with_uncertainty,
    build_event_assessments,
    calculate_dynamic_inputs,
    initialize_latent_state,
    initialize_uncertainty,
    model_variant_metadata,
    normalize_model_variant,
    prediction_interval,
    step_latent_state,
    step_uncertainty,
    stress_semantic_label,
    vitality_semantic_label,
)
from algorithm.micro_dynamics import (
    MicroDynamicState,
    apply_micro_dynamics,
    trigger_epiphany_refund,
)
from algorithm.physiology import clamp_energy, clamp_stress
from settings.model_defaults import (
    DEFAULT_INITIAL_ENERGY,
    DEFAULT_RANDOM_SEED,
    DEFAULT_TIME_STEP_MINUTES,
    RECOVERY_STATES,
)

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
    """Advance a daily trajectory with a nested CTSSM or legacy baseline."""
    def __init__(self, user: User, time_step: int = DEFAULT_TIME_STEP_MINUTES):
        """
        参数 user: 提供参数与策略；time_step: 积分步长（分钟），缺省读 user.params['time_step']。
        """
        self.user = user
        self.time_step = time_step or self.user.get_param("time_step", DEFAULT_TIME_STEP_MINUTES)
        seed = int(self.user.get_param("random_seed", DEFAULT_RANDOM_SEED))
        self.predictor = MarkovRegimePredictor(seed=seed, params=self.user.params)

    def update_user(self, user: User) -> None:
        """替换仿真对象并同步步长与马尔可夫参数表。"""
        self.user = user
        self.time_step = self.user.get_param("time_step", self.time_step)
        self.predictor.params = self.user.params

    def _feature_enabled(self, name: str) -> bool:
        """Read one opt-in mechanism flag from the versioned Phase 0 baseline."""
        flags = self.user.get_param("feature_flags", {})
        return bool(flags.get(name, False)) if isinstance(flags, dict) else False

    def _uses_ctssm(self) -> bool:
        """Return whether the paper-aligned latent-state model is active."""

        family = str(
            self.user.get_param(
                "model_family",
                "stress-ctssm.m0",
            )
        ).lower()
        return "ctssm" in family

    def _ctssm_variant(self) -> str:
        return normalize_model_variant(
            self.user.get_param("model_family", "stress-ctssm.m0")
        )

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
            if state in RECOVERY_STATES:
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
        
        # Phase 0 keeps the stochastic regime layer frozen. It can be enabled
        # later for a named ablation run without changing the baseline path.
        if self._feature_enabled("enable_regime_switching"):
            final_ds, final_de = self.predictor.apply_regime_modifiers(
                delta_S,
                delta_E,
                modifier_features,
                is_substep=is_substep,
            )
        else:
            final_ds, final_de = delta_S, delta_E
        
        return final_ds, final_de, base_ds, f_pen, components

    def simulate_day(self, events: List[BaseEvent], prev_S_end: Optional[float] = None,
                     prev_E_end: Optional[float] = None, date_str: str = None,
                     observations: Optional[List[Dict]] = None,
                     prev_P_end: Optional[float] = None,
                     prev_F_end: Optional[float] = None,
                     cross_day_transition: bool = False,
                     sleep_quality_deviation: float = 0.0,
                     cross_day_context: Optional[Dict] = None):
        """Run the active model while preserving the historical result tuple."""
        if self._uses_ctssm():
            return self._simulate_ctssm(
                events=events,
                prev_S_end=prev_S_end,
                prev_V_end=prev_E_end,
                prev_P_end=prev_P_end,
                prev_F_end=prev_F_end,
                date_str=date_str,
                observations=observations,
                cross_day_transition=cross_day_transition,
                sleep_quality_deviation=sleep_quality_deviation,
                cross_day_context=cross_day_context,
            )

        self.user._init_strategies()
        self.user.epoc_level = 0.0
        S_star = self.user.get_current_S_star()
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        
        micro_cfg = self.user.get_param("simulator_micro_params", {})
        macro_cfg = self.user.get_param("markov_semi_params", {})
        macro_interval = macro_cfg.get("regime_check_interval", 25)
            
        if prev_S_end is None:
            seed_val = int(self.user.get_param("random_seed", DEFAULT_RANDOM_SEED))
            rng_start = np.random.RandomState((seed_val + sum(ord(c) for c in date_str)) % (2**32 - 1))
            S = S_star + abs(rng_start.normal(0.0, S_star * 0.2))
        else:
            S = prev_S_end

        E = prev_E_end if prev_E_end is not None else DEFAULT_INITIAL_ENERGY

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

        # 微观化学离散状态池，具体释放/吸收规则在 algorithm.micro_dynamics。
        micro_state = MicroDynamicState()
        exhaustion_th = micro_cfg.get("energy_exhaustion_threshold", 20.0)

        prev_active_event_ids = set()
        minutes_since_last_macro_check = 0.0
        
        # 统一由 Predictor 的 RNG 负责生成所有随机源，确保可复现性
        date_hash = sum(ord(c) for c in date_str)
        rng_seed_val = int(self.user.get_param("random_seed", DEFAULT_RANDOM_SEED))
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
            if state not in RECOVERY_STATES:
                if prev_active_event_ids != current_active_ids:
                    trigger_reason = "任务切变中断"
                elif E < exhaustion_th and not energy_exhausted:
                    trigger_reason = "耗竭红线中断"
                elif minutes_since_last_macro_check >= macro_interval:
                    trigger_reason = f"宏观巡检({macro_interval}m)"

            if self._feature_enabled("enable_regime_switching") and (
                trigger_reason or current_time == base_date
            ):
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
                    
                    if (
                        old_regime == "FRICTION"
                        and new_regime == "FLOW"
                        and self._feature_enabled("enable_epiphany_refund")
                    ):
                        total_dopamine = trigger_epiphany_refund(
                            micro_state,
                            micro_cfg,
                            static_resilience,
                        )
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
                    rest_delay = micro_cfg.get("rest_penalty_recovery_delay", 5.0)
                    if (current_time - last_load_end_time).total_seconds() / 60.0 >= rest_delay:
                        continuous_load_hours = max(0.0, continuous_load_hours - (self.time_step / 60.0) * self.user.course_strategy.get_penalty_recovery_rate())
                    rest_session.tick(self.time_step)
                
                if routine_ev:
                    self._init_profiles([routine_ev], event_profile)

            if state in RECOVERY_STATES or (routine_ev and routine_ev.get_event_type() == "sleep"):
                sleep_elapsed_minutes += self.time_step
            else:
                sleep_elapsed_minutes = 0.0

            # --- RK4 积分，全子步共享相同的锁定噪声 ---
            def evaluate_for_rk4(stress_temp, energy_temp, sample_time, elapsed_for_step, is_substep):
                return self._evaluate_derivatives(
                    stress_temp,
                    energy_temp,
                    sample_time,
                    active_high_loads,
                    routine_ev,
                    state,
                    elapsed_for_step,
                    continuous_load_hours,
                    state_machine.sleep_eff,
                    micro_cfg,
                    step_noise_s=current_step_noise_s,
                    step_noise_e=current_step_noise_e,
                    is_substep=is_substep,
                )

            rk4_result = rk4_step(
                evaluate_for_rk4,
                S,
                E,
                current_time,
                self.time_step,
                sleep_elapsed_minutes,
            )
            delta_S = rk4_result.delta_s
            delta_E = rk4_result.delta_e
            base_ds_k1 = rk4_result.base_delta_s
            f_pen_k1 = rk4_result.fatigue_penalty

            routine_event_type = routine_ev.get_event_type() if routine_ev else None
            if self._feature_enabled("enable_micro_dynamics"):
                micro_result = apply_micro_dynamics(
                    pools=micro_state,
                    cfg=micro_cfg,
                    time_step=self.time_step,
                    state=state,
                    has_high_load=has_high_load,
                    routine_event_type=routine_event_type,
                    current_regime=self.predictor.current_regime,
                    current_stress=S,
                    current_energy=E,
                    stress_anchor=S_star,
                    raw_delta_s=delta_S,
                    raw_delta_e=delta_E,
                    base_delta_s=base_ds_k1,
                    inertia_delta_s=inertia_ds,
                    inertia_delta_e=inertia_de,
                    epoc_level=getattr(self.user, "epoc_level", 0.0),
                    resilience=static_resilience,
                    cur_str=cur_str,
                )
                self.user.epoc_level = micro_result.epoc_level
                trace_logs.extend(micro_result.logs)
                final_S_step = micro_result.final_s_step
                final_E_step = micro_result.final_e_step
                momentum_trace = micro_result.momentum_trace
            else:
                final_S_step = delta_S + inertia_ds
                final_E_step = delta_E + inertia_de
                momentum_trace = ""

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

            S = clamp_stress(S + final_S_step)
            E = clamp_energy(E + final_E_step)
            
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

    def _simulate_ctssm(
        self,
        *,
        events: List[BaseEvent],
        prev_S_end: Optional[float],
        prev_V_end: Optional[float],
        prev_P_end: Optional[float],
        prev_F_end: Optional[float],
        date_str: Optional[str],
        observations: Optional[List[Dict]],
        cross_day_transition: bool,
        sleep_quality_deviation: float,
        cross_day_context: Optional[Dict],
    ):
        """Run one paper-defined nested CTSSM candidate."""

        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        stress_baseline = self.user.get_current_S_star()
        model_variant = self._ctssm_variant()
        model_info = model_variant_metadata(model_variant)
        active_states = set(model_info["active_states"])
        ctssm_cfg = self.user.get_param("ctssm_params", {})
        vitality_baseline = float(
            ctssm_cfg.get(
                "vitality_baseline",
                DEFAULT_INITIAL_ENERGY,
            )
        )
        initial_stress = (
            float(prev_S_end)
            if prev_S_end is not None
            else stress_baseline
        )
        initial_vitality = (
            float(prev_V_end)
            if prev_V_end is not None
            else vitality_baseline
        )
        sleep_debt = self.user.get_sleep_debt()
        if prev_F_end is not None:
            initial_fatigue = max(0.0, min(1.0, float(prev_F_end)))
        else:
            initial_fatigue = max(
                0.0,
                min(
                    0.75,
                    max(0.0, vitality_baseline - initial_vitality) / 80.0
                    + min(0.25, sleep_debt * 0.04),
                ),
            )
        supplied_state = LatentState(
            stress=max(0.0, min(100.0, initial_stress)),
            vitality=max(0.0, min(100.0, initial_vitality)),
            perseverative_cognition=max(
                0.0,
                min(
                    1.0,
                    float(prev_P_end)
                    if prev_P_end is not None
                    else 0.0,
                ),
            ),
            recovery_debt=initial_fatigue,
        )
        if cross_day_transition:
            latent = initialize_latent_state(
                stress_baseline=stress_baseline,
                vitality_baseline=vitality_baseline,
                previous=supplied_state,
                sleep_quality_deviation=sleep_quality_deviation,
                config=ctssm_cfg,
                model_variant=model_variant,
            )
        else:
            latent = LatentState(
                stress=supplied_state.stress,
                vitality=(
                    supplied_state.vitality
                    if "V" in active_states
                    else vitality_baseline
                ),
                perseverative_cognition=(
                    supplied_state.perseverative_cognition
                    if "P" in active_states
                    else 0.0
                ),
                recovery_debt=(
                    supplied_state.recovery_debt
                    if "F" in active_states
                    else 0.0
                ),
            )
        uncertainty = initialize_uncertainty(ctssm_cfg, model_variant)

        timeline = TimelineManager(events, date_str)
        schedule = timeline.analyze_schedule()
        state_machine = PhysiologyStateMachine(schedule, params=self.user.params)
        assessments = build_event_assessments(events)
        base_date = datetime.strptime(date_str, "%Y-%m-%d")
        current_time = base_date
        end_of_day = base_date + timedelta(days=1) - timedelta(minutes=1)

        observations_by_time: Dict[str, List[Dict]] = {}
        for observation in observations or []:
            if not isinstance(observation, dict):
                continue
            raw_time = str(
                observation.get("time")
                or observation.get("target_time")
                or ""
            ).strip()
            if raw_time.endswith("Z"):
                raw_time = raw_time[:-1] + "+00:00"
            try:
                observed_time = datetime.fromisoformat(raw_time)
            except ValueError:
                # Clock-only observations are intentionally rejected. An EMA
                # must carry its calendar date so another day cannot reuse it.
                continue
            if len(raw_time) >= 10 and raw_time[4] == "-" and raw_time[7] == "-":
                key = observed_time.strftime("%Y-%m-%d %H:%M")
                normalized_observation = dict(observation)
                payload = observation.get("payload")
                if isinstance(payload, dict):
                    normalized_observation.update(payload)
                if "stress" not in normalized_observation:
                    normalized_observation["stress"] = normalized_observation.get(
                        "stress_0_10"
                    )
                if "vitality" not in normalized_observation:
                    normalized_observation["vitality"] = normalized_observation.get(
                        "vitality_0_10",
                        normalized_observation.get("energy_0_10"),
                    )
                if "perseverative_cognition" not in normalized_observation:
                    normalized_observation["perseverative_cognition"] = (
                        normalized_observation.get("perseverative_cognition_0_10")
                    )
                observations_by_time.setdefault(key, []).append(
                    normalized_observation
                )

        profiles: Dict[str, Dict] = {}
        for event in events:
            assessment = assessments[str(event.event_id)]
            start = str(event.start_time).split(" ")[-1][:5]
            end = str(event.end_time).split(" ")[-1][:5]
            profiles[str(event.event_id)] = {
                "event_id": str(event.event_id),
                "name": event.name,
                "type": assessment.event_type,
                "time": f"{start}-{end}",
                "detail": (
                    f"评价压力={assessment.stress_intensity:.2f}; "
                    f"任务要求={assessment.task_demand:.2f}; "
                    f"恢复体验={assessment.recovery_quality:.2f}"
                ),
                "total_S": 0.0,
                "total_E": 0.0,
                "base_S": 0.0,
                "penalty_S": 0.0,
                "weight_factor": (
                    f"U={assessment.stress_intensity:.2f}, "
                    f"D={assessment.task_demand:.2f}"
                ),
                "credits": event.metadata.get("credits", "N/A"),
                "hours": event.metadata.get("hours", "N/A"),
                "level_str": event.metadata.get("level_str", "N/A"),
                "math_trace": (
                    "事件客观属性 + 主观评价先验 → U(t), D(t), R(t); "
                    f"状态按 {model_info['canonical']} 连续时间方程推进。"
                ),
                "assessment": {
                    "objective": assessment.objective,
                    "appraisal": assessment.appraisal,
                    "stress_intensity": round(assessment.stress_intensity, 4),
                    "task_demand": round(assessment.task_demand, 4),
                    "recovery_quality": round(assessment.recovery_quality, 4),
                    "pre_weight": round(assessment.pre_weight, 4),
                    "post_weight": round(assessment.post_weight, 4),
                    "onset_floor": round(assessment.onset_floor, 4),
                    "semantic": assessment.semantic,
                },
            }

        results: List[Dict] = []
        trace_logs = [
            (
                f"[00:00] {model_info['label']} 启动："
                f"活跃状态={','.join(model_info['active_states'])}，"
                f"步长={self.time_step}m，压力基线={stress_baseline:.1f}"
            ),
            (
                "[说明] 日历仅提供事件评价先验；P 为持续性认知代理，"
                "F 为恢复债代理，均不用于医学诊断。"
            ),
        ]
        unfinished_tasks = (
            cross_day_context.get("unfinished_tasks", [])
            if isinstance(cross_day_context, dict)
            else []
        )
        if unfinished_tasks:
            trace_logs.append(
                "[跨日上下文] 仅对明确未完成任务施加有界、逐日衰减的背景输入："
                + "、".join(
                    str(item.get("event_name") or "未命名任务")
                    for item in unfinished_tasks[:5]
                    if isinstance(item, dict)
                )
            )
        continuous_load_hours = 0.0
        wake_s = latent.stress
        wake_recorded = False
        boundary_hits = 0

        while current_time <= end_of_day:
            cur_str = current_time.strftime("%H:%M")
            current_observation_key = current_time.strftime("%Y-%m-%d %H:%M")
            active_high_loads = timeline.get_active_high_load_events(current_time)
            routine_ev = timeline.get_active_routine(current_time)
            has_high_load = bool(active_high_loads)
            state_name, inertia_ds, inertia_dv, state_logs = (
                state_machine.determine_state(
                    current_time,
                    has_high_load,
                    routine_ev,
                )
            )
            trace_logs.extend(state_logs)

            dynamic_inputs = calculate_dynamic_inputs(
                events,
                assessments,
                current_time,
                date_str,
                sleep_appraisal_shift=(
                    float(
                        ctssm_cfg.get(
                            "sleep_quality_event_appraisal_gain",
                            0.08,
                        )
                    )
                    * float(sleep_quality_deviation)
                ),
            )
            sleeping = state_name in RECOVERY_STATES
            cross_day_unfinished_input = self._cross_day_unfinished_input(
                cross_day_context,
                current_time=current_time,
                base_date=base_date,
                sleeping=sleeping,
                config=ctssm_cfg,
            )
            if cross_day_unfinished_input > 0.0:
                dynamic_inputs = DynamicInputs(
                    event_stress=dynamic_inputs.event_stress,
                    task_demand=dynamic_inputs.task_demand,
                    recovery=dynamic_inputs.recovery,
                    anticipatory_input=dynamic_inputs.anticipatory_input,
                    post_event_input=1.0
                    - (1.0 - dynamic_inputs.post_event_input)
                    * (1.0 - cross_day_unfinished_input),
                    active_event_ids=dynamic_inputs.active_event_ids,
                    active_event_names=dynamic_inputs.active_event_names,
                )
            if sleeping and dynamic_inputs.recovery < 0.85:
                dynamic_inputs = DynamicInputs(
                    event_stress=dynamic_inputs.event_stress,
                    task_demand=dynamic_inputs.task_demand,
                    recovery=0.85,
                    anticipatory_input=dynamic_inputs.anticipatory_input,
                    post_event_input=dynamic_inputs.post_event_input,
                    active_event_ids=dynamic_inputs.active_event_ids,
                    active_event_names=dynamic_inputs.active_event_names,
                )
            if inertia_ds > 0.0:
                interruption_input = max(0.0, min(1.0, inertia_ds / 10.0))
                dynamic_inputs = DynamicInputs(
                    event_stress=1.0
                    - (1.0 - dynamic_inputs.event_stress)
                    * (1.0 - interruption_input),
                    task_demand=dynamic_inputs.task_demand,
                    recovery=dynamic_inputs.recovery,
                    anticipatory_input=dynamic_inputs.anticipatory_input,
                    post_event_input=dynamic_inputs.post_event_input,
                    active_event_ids=dynamic_inputs.active_event_ids,
                    active_event_names=dynamic_inputs.active_event_names,
                )

            previous = latent
            latent, diagnostics = step_latent_state(
                latent,
                dynamic_inputs,
                current_time=current_time,
                dt_minutes=self.time_step,
                stress_baseline=stress_baseline,
                sleep_debt_hours=sleep_debt,
                config=ctssm_cfg,
                sleeping=sleeping,
                model_variant=model_variant,
            )
            uncertainty = step_uncertainty(
                uncertainty,
                diagnostics=diagnostics,
                dt_minutes=self.time_step,
                config=ctssm_cfg,
                model_variant=model_variant,
            )

            observation_applied = False
            for observation in observations_by_time.get(
                current_observation_key, []
            ):
                try:
                    latent, uncertainty = assimilate_observation_with_uncertainty(
                        latent,
                        uncertainty,
                        observation,
                        config=ctssm_cfg,
                        model_variant=model_variant,
                    )
                    observation_applied = True
                except (TypeError, ValueError):
                    trace_logs.append(
                        f"[{cur_str}] 忽略无法解析的 EMA 观测。"
                    )

            if not wake_recorded and current_time >= schedule["wake_time"]:
                wake_s = latent.stress
                wake_recorded = True
                trace_logs.append(
                    f"[{cur_str}] 清晨潜在压力参考={wake_s:.1f}"
                )

            dt_hours = self.time_step / 60.0
            if dynamic_inputs.task_demand > 0.05:
                continuous_load_hours += (
                    dynamic_inputs.task_demand * dt_hours
                )
            else:
                continuous_load_hours = max(
                    0.0,
                    continuous_load_hours
                    - (0.35 + dynamic_inputs.recovery) * dt_hours,
                )

            active_ids = list(dynamic_inputs.active_event_ids)
            for event_id in active_ids:
                assessment = assessments[event_id]
                profile = profiles[event_id]
                profile["total_S"] += (
                    assessment.stress_intensity
                    * float(ctssm_cfg.get("event_stress_gain", 30.0))
                    * dt_hours
                )
                profile["total_E"] -= (
                    assessment.task_demand
                    * float(
                        ctssm_cfg.get(
                            "demand_vitality_drain_per_hour",
                            13.0,
                        )
                    )
                    * dt_hours
                )

            delta_s = latent.stress - previous.stress
            delta_v = latent.vitality - previous.vitality
            stress_interval = prediction_interval(
                latent.stress,
                uncertainty.stress_variance,
                lower_bound=0.0,
                upper_bound=100.0,
            )
            vitality_interval = prediction_interval(
                latent.vitality,
                uncertainty.vitality_variance,
                lower_bound=0.0,
                upper_bound=100.0,
            )
            if latent.stress <= 0.001 or latent.stress >= 99.999:
                boundary_hits += 1
            dominant_stressors = [
                item["name"]
                for item in sorted(
                    profiles.values(),
                    key=lambda profile: profile["total_S"],
                    reverse=True,
                )[:2]
                if item["total_S"] > 0.0
            ]
            if cross_day_unfinished_input >= 0.10:
                for task in unfinished_tasks[:2]:
                    if not isinstance(task, dict):
                        continue
                    name = str(task.get("event_name") or "昨日未完成任务")
                    if name not in dominant_stressors:
                        dominant_stressors.append(name)

            results.append(
                {
                    "time": cur_str,
                    "S": latent.stress,
                    "V": latent.vitality,
                    "E": latent.vitality,
                    "P": latent.perseverative_cognition,
                    "F": latent.recovery_debt,
                    "state": state_name,
                    "delta_S": delta_s,
                    "delta_V": delta_v,
                    "delta_E": delta_v,
                    "f_pen": latent.recovery_debt,
                    "continuous_hours": continuous_load_hours,
                    "current_events": list(
                        dynamic_inputs.active_event_names
                    ),
                    "dominant_stressors": dominant_stressors,
                    "event_stress_input": dynamic_inputs.event_stress,
                    "task_demand": dynamic_inputs.task_demand,
                    "recovery_input": dynamic_inputs.recovery,
                    "anticipatory_input": (
                        dynamic_inputs.anticipatory_input
                    ),
                    "post_event_input": dynamic_inputs.post_event_input,
                    "cross_day_unfinished_input": cross_day_unfinished_input,
                    "cross_day_context_names": [
                        str(item.get("event_name") or "")
                        for item in unfinished_tasks[:5]
                        if isinstance(item, dict) and item.get("event_name")
                    ],
                    "stress_equilibrium": diagnostics[
                        "stress_equilibrium"
                    ],
                    "stress_baseline": stress_baseline,
                    "vitality_equilibrium": diagnostics[
                        "vitality_equilibrium"
                    ],
                    "model_variant": model_info["canonical"],
                    "active_states": model_info["active_states"],
                    "stress_interval_90": {
                        "lower": stress_interval[0],
                        "upper": stress_interval[1],
                    },
                    "vitality_interval_90": (
                        {
                            "lower": vitality_interval[0],
                            "upper": vitality_interval[1],
                        }
                        if "V" in active_states
                        else None
                    ),
                    "stress_label": stress_semantic_label(
                        latent.stress
                    ),
                    "vitality_label": vitality_semantic_label(
                        latent.vitality
                    ),
                    "observation_assimilated": observation_applied,
                }
            )
            current_time += timedelta(minutes=self.time_step)

        alerts, confidence_series = AlertMonitor(self.user.params).analyze(
            results
        )
        hit_rate = boundary_hits / max(1, len(results))
        trace_logs.append(
            (
                f"[23:59] {model_info['canonical']} 结束：S={latent.stress:.1f}, "
                f"V={latent.vitality:.1f}, P={latent.perseverative_cognition:.2f}, "
                f"F={latent.recovery_debt:.2f}, 边界命中率={hit_rate:.2%}, "
                f"关怀提示={len(alerts)}"
            )
        )

        trajectory_diagnostics = build_event_trajectory_diagnostics(
            results,
            events,
            assessments,
        )
        profile_list = []
        for profile in profiles.values():
            profile_list.append(
                {
                    **{
                        key: value
                        for key, value in profile.items()
                        if key
                        not in {
                            "total_S",
                            "total_E",
                            "base_S",
                            "penalty_S",
                        }
                    },
                    "s_impact": round(profile["total_S"], 2),
                    "e_impact": round(profile["total_E"], 2),
                    "base_s": round(profile["base_S"], 2),
                    "penalty_s": round(profile["penalty_S"], 2),
                    "trajectory": trajectory_diagnostics.get(
                        str(profile.get("event_id")),
                    ),
                }
            )

        return (
            results,
            latent.stress,
            latent.vitality,
            schedule["wake_time"],
            [
                schedule["late_night_active_end"],
                schedule["night_sleep_start"],
            ],
            alerts,
            confidence_series,
            trace_logs,
            profile_list,
            wake_s,
        )

    @staticmethod
    def _cross_day_unfinished_input(
        context: Optional[Dict],
        *,
        current_time: datetime,
        base_date: datetime,
        sleeping: bool,
        config: Dict,
    ) -> float:
        """Map explicit unfinished-task context to a small decaying input.

        This is deliberately not an active event.  It represents background
        cognitive carryover and is capped so a title or API label cannot keep
        the whole next day at a high-pressure equilibrium.
        """

        if not isinstance(context, dict):
            return 0.0
        tasks = context.get("unfinished_tasks", [])
        if not isinstance(tasks, list) or not tasks:
            return 0.0
        try:
            load = max(0.0, min(0.90, float(context.get("unfinished_load", 0.0))))
        except (TypeError, ValueError):
            return 0.0
        if load <= 0.0:
            return 0.0
        elapsed_hours = max(
            0.0,
            (current_time - base_date).total_seconds() / 3600.0,
        )
        decay_hours = max(
            4.0,
            min(48.0, float(config.get("cross_day_unfinished_decay_hours", 18.0))),
        )
        floor = max(
            0.0,
            min(0.45, float(config.get("cross_day_unfinished_input_floor", 0.18))),
        )
        value = load * (
            floor + (1.0 - floor) * math.exp(-elapsed_hours / decay_hours)
        )
        if sleeping:
            value *= max(
                0.0,
                min(
                    1.0,
                    float(
                        config.get(
                            "cross_day_unfinished_sleep_multiplier",
                            0.25,
                        )
                    ),
                ),
            )
        return max(0.0, min(0.65, value))

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
