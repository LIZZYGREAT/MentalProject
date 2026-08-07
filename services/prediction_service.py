"""Request-independent daily prediction service shared by Web and bot entry points."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from typing import Any, Callable, Dict, Optional

from algorithm.time_utils import normalize_interval
from auth.database import AppDatabase
from data_pipeline.orchestrator import inject_routine_events
from entity.user import User
from services.cross_day_context import (
    build_automatic_cross_day_context,
    semantic_context_from_cross_day,
)
from services.onboarding import (
    FEATURE_VERSION,
    MAPPING_VERSION,
    MODEL_VERSION,
    PARAMETER_VERSION,
    QUESTIONNAIRE_DEFINITION,
    build_daily_context,
    build_routine_plan,
    new_id,
    utc_now,
)
from settings.model_defaults import BASE_DATA_DIR, DEFAULT_INITIAL_ENERGY, FEISHU_REQUEST_TIMEOUT_SECONDS
from utils.event_factory import EventFactory
from utils.get_token import FeishuAPI


class PredictionServiceError(RuntimeError):
    code = "prediction_error"


class OnboardingRequiredError(PredictionServiceError):
    code = "onboarding_required"


class InactiveUserError(PredictionServiceError):
    code = "inactive_user"


def default_feishu_token_path(user_id: int) -> str:
    token_dir = os.path.join(BASE_DATA_DIR, "user_tokens")
    os.makedirs(token_dir, exist_ok=True)
    return os.path.join(token_dir, f"user_{int(user_id)}.json")


def _event_windows(events: list[Any]) -> list[Dict[str, str]]:
    windows = []
    for event in events:
        try:
            start, end = normalize_interval(event.start_time, event.end_time)
        except (AttributeError, TypeError, ValueError):
            continue
        windows.append({"start": start, "end": end})
    return windows


def _apply_profile_routine(user: User, profile: Dict[str, Any]) -> None:
    routine = profile.get("routine") or {}
    if not isinstance(routine, dict):
        return
    cfg = dict(user.get_param("routine_weaver", {}) or {})
    for key, config_start, config_end in (
        ("lunch_ideal_time", "lunch_ideal_start", "lunch_ideal_end"),
        ("dinner_ideal_time", "dinner_ideal_start", "dinner_ideal_end"),
    ):
        value = routine.get(key)
        if not value:
            continue
        minutes = int(value[:2]) * 60 + int(value[3:])
        cfg[config_start] = value
        cfg[config_end] = f"{(minutes + 30) // 60:02d}:{(minutes + 30) % 60:02d}"
    if routine.get("weekday_wake_time"):
        user.params["default_wake_time"] = routine["weekday_wake_time"]
    if routine.get("weekday_sleep_start"):
        user.params["default_sleep_time"] = routine["weekday_sleep_start"]
    user.params["routine_weaver"] = cfg

    allowed_paths = set(QUESTIONNAIRE_DEFINITION.get("parameter_whitelist", []))
    applied_priors = []
    if profile.get("mapping_version") == MAPPING_VERSION:
        from calibration.parameter_validation import get_nested, set_nested

        for prior in profile.get("parameter_priors", []):
            if not isinstance(prior, dict):
                continue
            path = str(prior.get("parameter") or "")
            current = get_nested(user.params, path)
            if path not in allowed_paths or current is None:
                continue
            try:
                group_mean = float(current)
                questionnaire_mean = float(prior["mean"])
            except (KeyError, TypeError, ValueError):
                continue
            runtime_value = 0.65 * group_mean + 0.35 * questionnaire_mean
            set_nested(user.params, path, runtime_value)
            applied_priors.append({"parameter": path, "runtime_prior_mean": runtime_value})
    user.params["individual_parameter_priors"] = applied_priors
    if "ctssm" not in str(user.params.get("model_family", "")).lower():
        user._init_strategies()
    user.solver.update_user(user)


class PredictionService:
    """Run the existing model without Flask session or request globals."""

    def __init__(
        self,
        database: AppDatabase,
        *,
        token_path_factory: Callable[[int], str] = default_feishu_token_path,
        feishu_api_factory: Callable[..., FeishuAPI] = FeishuAPI,
        calendar_fetcher: Optional[Callable[..., list]] = None,
    ):
        self.database = database
        self.token_path_factory = token_path_factory
        self.feishu_api_factory = feishu_api_factory
        self.calendar_fetcher = calendar_fetcher

    def run_daily_prediction(
        self,
        user_id: int,
        target_date: str,
        force_calendar_refresh: bool = False,
        observations: Optional[list] = None,
    ) -> Dict[str, Any]:
        user_id = int(user_id)
        user_record = self.database.get_user(user_id)
        if not user_record or not user_record.get("is_active"):
            raise InactiveUserError("项目账号不可用")
        try:
            datetime.strptime(str(target_date), "%Y-%m-%d")
        except ValueError as exc:
            raise PredictionServiceError("target_date 必须使用 YYYY-MM-DD") from exc

        profile = self.database.latest_profile_snapshot(user_id)
        if not profile:
            raise OnboardingRequiredError("请先在 Web 页面完成初始化问卷")
        params = User._params_from_json_safe(self.database.load_user_params(user_id))
        model_user = User(user_id=str(user_id), params=params, load_from_file=False)
        _apply_profile_routine(model_user, profile)

        calendar_events: list[Dict[str, Any]] = []
        calendar_connected = False
        calendar_error: Optional[str] = None
        try:
            fetcher = self.calendar_fetcher
            if fetcher is None:
                from data_pipeline.fetcher import fetch_events_with_timeout

                fetcher = fetch_events_with_timeout
            api = self.feishu_api_factory()
            token_info, _ = api.ensure_valid_token(self.token_path_factory(user_id))
            calendar_events = fetcher(
                date_str=str(target_date),
                injected_token=token_info["access_token"],
                timeout=FEISHU_REQUEST_TIMEOUT_SECONDS,
                force_refresh=bool(force_calendar_refresh),
                cache_namespace=f"user:{user_id}",
            )
            calendar_connected = True
        except Exception as exc:  # calendar is an explicitly supported degradation path
            calendar_error = type(exc).__name__

        events = EventFactory.create_from_json(calendar_events)
        plan = build_routine_plan(
            profile,
            target_date=str(target_date),
            occupied_windows=_event_windows(events),
        )
        self.database.save_routine_plan(user_id, plan)
        context = build_daily_context(profile, plan, str(target_date))
        self.database.save_daily_context(user_id, context)
        try:
            final_events = inject_routine_events(events, str(target_date), model_user)
        except Exception:
            final_events = events

        cross_day_context = build_automatic_cross_day_context(
            self.database,
            user_id,
            str(target_date),
            max_carry_days=int(os.getenv("CROSS_DAY_UNFINISHED_MAX_DAYS", "3")),
        )
        previous_day_state = dict(
            (cross_day_context or {}).get("previous_day_state") or {}
        )
        if context.get("context_snapshot_id"):
            updated = self.database.update_daily_context_previous_day(
                user_id,
                context["context_snapshot_id"],
                cross_day_context,
            )
            if updated:
                context = updated
        semantic_context = semantic_context_from_cross_day(cross_day_context)
        if semantic_context:
            for event in final_events:
                if not isinstance(getattr(event, "metadata", None), dict):
                    event.metadata = {}
                event.metadata.setdefault("semantic_context", dict(semantic_context))

        init_s = previous_day_state.get("S_end", model_user.get_current_S_star())
        init_e = previous_day_state.get(
            "V_end",
            previous_day_state.get("E_end", DEFAULT_INITIAL_ENERGY),
        )
        init_p = previous_day_state.get("P_end")
        init_f = previous_day_state.get("F_end")
        if previous_day_state.get("sleep_debt") is not None:
            model_user.set_sleep_debt(float(previous_day_state["sleep_debt"]))

        stored_observations = self._stored_observations(user_id, str(target_date))
        all_observations = [*stored_observations, *(observations or [])]
        result_tuple = model_user.solver.simulate_day(
            final_events,
            float(init_s),
            float(init_e),
            str(target_date),
            observations=all_observations,
            prev_P_end=None if init_p is None else float(init_p),
            prev_F_end=None if init_f is None else float(init_f),
            cross_day_transition=bool(previous_day_state),
            sleep_quality_deviation=0.0,
            cross_day_context=cross_day_context,
        )
        results, end_s, end_e, _, _, alerts, _, solver_logs, profiles, _ = result_tuple
        prediction_run_id = new_id()
        seed = int(model_user.get_param("random_seed", 42))
        result_summary = {
            "end_S": round(float(end_s), 4),
            "end_E": round(float(end_e), 4),
            "end_V": round(float(end_e), 4),
            "end_P": round(float(results[-1].get("P", 0.0)), 4) if results else 0.0,
            "end_F": round(float(results[-1].get("F", 0.0)), 4) if results else 0.0,
            "alerts": alerts,
            "point_count": len(results),
            "baseline_S": model_user.get_current_S_star(),
            "stress_threshold": model_user.get_current_threshold(),
            "model_variant": results[-1].get("model_variant") if results else None,
            "active_states": results[-1].get("active_states", ["S"]) if results else ["S"],
        }
        run_input = {
            "date": str(target_date),
            "calendar_event_count": len(calendar_events),
            "calendar_connected": calendar_connected,
            "observations": all_observations,
            "stored_observation_count": len(stored_observations),
            "cross_day_context": cross_day_context,
            "random_seed": seed,
            "profile_snapshot_id": profile.get("profile_snapshot_id"),
            "routine_plan_id": plan.get("routine_plan_id"),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {"input": run_input, "result": result_summary},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.database.save_prediction_run(
            user_id,
            {
                "prediction_run_id": prediction_run_id,
                "context_snapshot_id": context.get("context_snapshot_id"),
                "local_date": str(target_date),
                "schema_version": "prediction_run.v1",
                "model_version": MODEL_VERSION,
                "parameter_version": PARAMETER_VERSION,
                "feature_version": FEATURE_VERSION,
                "random_seed": seed,
                "input": run_input,
                "result": {**result_summary, "fingerprint": fingerprint},
                "created_at": utc_now(),
                "diagnostics": {
                    "schema_version": "prediction_diagnostics.v2",
                    "event_profiles": profiles,
                    "trace_logs": solver_logs,
                    "calendar_error_type": calendar_error,
                },
            },
            results,
        )
        return {
            "prediction_run_id": prediction_run_id,
            "local_date": str(target_date),
            "result": result_summary,
            "calendar_connected": calendar_connected,
            "calendar_degraded": not calendar_connected,
        }

    def _stored_observations(self, user_id: int, target_date: str) -> list[Dict[str, Any]]:
        observations = []
        for item in self.database.list_feedback_observations(
            user_id,
            target_date=target_date,
            limit=200,
        ):
            if item.get("feedback_type") != "momentary_state":
                continue
            payload = item.get("payload") or {}
            observations.append(
                {
                    "target_time": item.get("target_time") or item.get("reported_at"),
                    "stress": payload.get("stress_0_10"),
                    "vitality": payload.get("vitality_0_10", payload.get("energy_0_10")),
                    "perseverative_cognition": payload.get("perseverative_cognition_0_10"),
                    "retrospective": bool(item.get("retrospective")),
                    "feedback_id": item.get("feedback_id"),
                }
            )
        return observations
