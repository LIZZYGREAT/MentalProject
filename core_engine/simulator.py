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
from algorithm.time_utils import normalize_observation_to_model_step
from settings.model_defaults import (
    DEFAULT_INITIAL_ENERGY,
    DEFAULT_TIME_STEP_MINUTES,
    RECOVERY_STATES,
)

class Simulator:
    """Advance the production paper-aligned CTSSM trajectory."""
    def __init__(self, user: User, time_step: int = DEFAULT_TIME_STEP_MINUTES):
        self.user = user
        self.time_step = time_step or self.user.get_param(
            "time_step", DEFAULT_TIME_STEP_MINUTES
        )

    def _ctssm_variant(self) -> str:
        return normalize_model_variant(
            self.user.get_param("model_family", "stress-ctssm.m0")
        )

    def simulate_day(
        self,
        events: List[BaseEvent],
        prev_S_end: Optional[float] = None,
        prev_E_end: Optional[float] = None,
        date_str: str = None,
        observations: Optional[List[Dict]] = None,
        prev_P_end: Optional[float] = None,
        prev_F_end: Optional[float] = None,
        cross_day_transition: bool = False,
        sleep_quality_deviation: float = 0.0,
        cross_day_context: Optional[Dict] = None,
    ):
        """Run the production CTSSM while preserving the result tuple contract."""
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
            observed_time = normalize_observation_to_model_step(
                raw_time,
                step_minutes=self.time_step,
                target_date=base_date.date(),
            )
            if observed_time is None:
                # Clock-only, cross-date, and after-last-grid observations are
                # intentionally rejected instead of being moved backwards.
                continue
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

            # A trajectory point is the posterior state at its timestamp.
            # Propagation with inputs at t advances a separate terminal state
            # over [t, t + dt), which becomes the next trajectory point.
            point_latent = latent
            point_uncertainty = uncertainty
            latent, diagnostics = step_latent_state(
                point_latent,
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
                point_uncertainty,
                diagnostics=diagnostics,
                dt_minutes=self.time_step,
                config=ctssm_cfg,
                model_variant=model_variant,
            )

            if not wake_recorded and current_time >= schedule["wake_time"]:
                wake_s = point_latent.stress
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

            delta_s = point_latent.stress - previous.stress
            delta_v = point_latent.vitality - previous.vitality
            stress_interval = prediction_interval(
                point_latent.stress,
                point_uncertainty.stress_variance,
                lower_bound=0.0,
                upper_bound=100.0,
            )
            vitality_interval = prediction_interval(
                point_latent.vitality,
                point_uncertainty.vitality_variance,
                lower_bound=0.0,
                upper_bound=100.0,
            )
            if point_latent.stress <= 0.001 or point_latent.stress >= 99.999:
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
                    "S": point_latent.stress,
                    "V": point_latent.vitality,
                    "E": point_latent.vitality,
                    "P": point_latent.perseverative_cognition,
                    "F": point_latent.recovery_debt,
                    "state": state_name,
                    "delta_S": delta_s,
                    "delta_V": delta_v,
                    "delta_E": delta_v,
                    "f_pen": point_latent.recovery_debt,
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
                        point_latent.stress
                    ),
                    "vitality_label": vitality_semantic_label(
                        point_latent.vitality
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
