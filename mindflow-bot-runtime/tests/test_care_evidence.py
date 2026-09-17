from datetime import date

from app.services.care_evidence import CareEvidenceBuilder


TARGET = date(2030, 1, 15)


def _event(event_id, name, start, end, prior, *, event_type="course", **extra):
    return {
        "id": event_id,
        "display_name": name,
        "summary": name,
        "event_type": event_type,
        "start_time": f"{TARGET.isoformat()}T{start}:00+08:00",
        "end_time": f"{TARGET.isoformat()}T{end}:00+08:00",
        "workload_prior": prior,
        "semantic_values": {
            "difficulty": min(1.0, prior),
            "cognitive_demand": min(1.0, prior + 0.05),
        },
        **extra,
    }


def _packet(events, *, output=None, profile=None, preferences=None, longitudinal_state=None):
    return CareEvidenceBuilder("Asia/Shanghai").build(
        source="forecast_warning",
        local_date=TARGET,
        alert={"time": "10:40", "S": 8.1, "V": 4.6, "F": 0.63},
        forecast_output=output,
        calendar_events=events,
        longitudinal_state=longitudinal_state,
        profile=profile,
        care_preferences=preferences,
    )


def test_dense_high_load_course_block_uses_continuity_and_weighted_load():
    packet = _packet([
        _event("1", "高等数学", "08:00", "08:45", 0.82),
        _event("2", "数据结构", "08:55", "09:40", 0.82),
        _event("3", "操作系统", "10:00", "10:45", 0.75),
        _event("4", "计算机网络", "10:55", "11:40", 0.80),
    ])

    assert packet.schedule["consecutive_course_count"] == 4
    assert packet.schedule["largest_break_minutes"] == 20
    assert packet.schedule["weighted_load"] > 0.7
    assert packet.reason_candidates[0]["code"] == "dense_high_load_course_block"


def test_many_simple_tasks_do_not_become_a_high_load_reason():
    packet = _packet([
        _event("1", "拿快递", "08:00", "08:10", 0.15, event_type="task"),
        _event("2", "吃饭", "08:20", "08:50", 0.10, event_type="meal"),
        _event("3", "签到", "09:00", "09:05", 0.08, event_type="task"),
        _event("4", "取打印件", "09:20", "09:30", 0.12, event_type="task"),
    ])

    assert packet.schedule["high_load_event_count"] == 0
    assert all(
        item["code"] != "dense_high_load_course_block"
        for item in packet.reason_candidates
    )


def test_two_low_load_courses_do_not_create_dense_high_load_reason():
    packet = _packet([
        _event("course-a", "通识选修 A", "08:00", "08:45", 0.30),
        _event("course-b", "轻量选修 B", "08:55", "09:40", 0.32),
    ])

    assert packet.schedule["continuous_course_blocks"][0]["high_load_course_count"] == 0
    assert "dense_high_load_course_block" not in {
        item["code"] for item in packet.reason_candidates
    }


def test_high_task_plus_two_low_courses_does_not_mislabel_task_as_course_reason():
    packet = _packet([
        _event("course-a", "通识选修 A", "08:00", "08:45", 0.30),
        _event("course-b", "轻量选修 B", "08:55", "09:40", 0.32),
        _event("ddl-task", "项目 DDL", "10:00", "10:30", 0.90, event_type="task"),
    ])

    dense = [item for item in packet.reason_candidates if item["code"] == "dense_high_load_course_block"]
    assert dense == []
    assert all("ddl-task" not in item.get("fact_ids", []) for item in packet.reason_candidates)


def test_mixed_course_block_uses_only_block_course_fact_ids():
    packet = _packet([
        _event("course-a", "高等数学", "08:00", "08:45", 0.82),
        _event("course-b", "数据结构", "08:55", "09:40", 0.78),
        _event("course-c", "轻量选修", "10:00", "10:45", 0.31),
        _event("task", "高负荷任务", "10:55", "11:25", 0.95, event_type="task"),
    ])

    dense = next(item for item in packet.reason_candidates if item["code"] == "dense_high_load_course_block")
    assert set(dense["fact_ids"]) == {"event:course-a", "event:course-b", "event:course-c"}
    assert "event:task" not in dense["fact_ids"]


def test_bounded_packet_keeps_afternoon_risk_events_after_many_earlier_events():
    events = [
        _event(f"early-{index}", f"早间安排 {index}", f"{index:02d}:00", f"{index:02d}:20", 0.20, event_type="task")
        for index in range(8)
    ]
    events.extend([
        _event("risk-1", "下午课程", "14:00", "14:45", 0.72),
        _event("risk-2", "下午任务", "15:00", "15:30", 0.75, event_type="task"),
    ])
    packet = CareEvidenceBuilder("Asia/Shanghai").build(
        source="forecast_warning",
        local_date=TARGET,
        alert={"time": "15:00", "S": 8.1, "V": 4.6, "F": 0.63},
        calendar_events=events,
    )

    fact_ids = packet.fact_ids
    assert {"event:risk-1", "event:risk-2"} <= fact_ids
    assert len(packet.event_facts) <= 8
    assert all(
        set(reason.get("fact_ids") or []) <= fact_ids
        for reason in packet.reason_candidates
    )


def test_time_bounded_longitudinal_state_adds_recovery_trend_evidence():
    packet = _packet(
        [],
        longitudinal_state={
            "features": [
                {
                    "feature": "recovery_trend",
                    "value": "declining",
                    "time_window": "7d",
                    "source_at": f"{TARGET.isoformat()}T08:00:00+08:00",
                    "valid_until": f"{TARGET.isoformat()}T18:00:00+08:00",
                    "source": "participant_slow_state",
                    "evidence_type": "derived_trend",
                    "model_version": "research-state-v1",
                },
                {
                    "feature": "recent_workload",
                    "value": "high",
                    "time_window": "7d",
                    "source_at": f"{TARGET.isoformat()}T08:00:00+08:00",
                    "valid_until": f"{TARGET.isoformat()}T09:00:00+08:00",
                },
            ]
        },
    )

    assert packet.longitudinal_state["recovery_trend"] == "declining"
    assert packet.longitudinal_state["recent_workload_7d"] is None
    assert "declining_recovery_trend" in {
        item["code"] for item in packet.reason_candidates
    }


def test_course_catalog_is_kept_only_for_a_unique_confirmed_match():
    unique = _packet([
        _event(
            "1", "数据结构", "08:00", "08:45", 0.82,
            course_name="数据结构",
            course_code="CS201",
            course_match_confidence=0.94,
            course_catalog={
                "canonical_name": "数据结构",
                "code": "CS201",
                "credits": 4.0,
                "hours": 68.0,
                "hours_per_week": 4.0,
            },
        )
    ])
    assert unique.event_facts[0]["course_catalog"]["credits"] == 4.0

    ambiguous = _packet([
        _event(
            "1", "数据结构", "08:00", "08:45", 0.82,
            course_name="数据结构",
            course_match_confidence=0.50,
            course_catalog={"canonical_name": "数据结构", "code": "CS201"},
        )
    ])
    assert ambiguous.event_facts[0]["course_catalog"] is None


def test_previous_day_carryover_is_prediction_provenance_not_observation():
    packet = _packet(
        [_event("1", "普通安排", "10:00", "10:30", 0.2, event_type="task")],
        output={
            "initial_state": {
                "mode": "previous_day_forecast",
                "stress_0_10": 6.2,
                "vitality_0_10": 4.9,
            }
        },
        profile={"model_params": {"S_star_init": 50.0}},
    )

    assert "previous_day_carryover" in {
        item["code"] for item in packet.reason_candidates
    }
    assert packet.trajectory["previous_day_carryover"]["source"] == "previous_day_forecast"


def test_unpromoted_profile_does_not_emit_personal_sensitivity():
    packet = _packet(
        [_event("1", "数据结构", "10:00", "10:45", 0.82)],
        profile={"model_params": {"workload_sensitivity_i": 4.5}},
    )

    assert packet.personalization["available"] is False
    assert "high_personal_workload_sensitivity" not in {
        item["code"] for item in packet.reason_candidates
    }


def test_promoted_profile_reads_hierarchical_parameters_and_stored_population_prior():
    packet = _packet(
        [_event("1", "数据结构", "10:00", "10:45", 0.82)],
        profile={
            "model_params": {
                "hierarchical_parameters": {
                    "workload_sensitivity_i": 4.2,
                    "stress_recovery_rate_i": 0.36,
                    "stress_reactivity_i": 0.92,
                },
                "hierarchical_population_prior": {
                    "workload_sensitivity_i": {"mean": 2.9},
                    "stress_recovery_rate_i": {"mean": 0.51},
                    "stress_reactivity_i": {"mean": 0.7},
                },
            },
            "runtime_model_provenance": {"provenance_type": "stage5_promotion"},
        },
    )

    assert packet.personalization["workload_sensitivity"] == "above_population_prior"
    assert packet.personalization["recovery_rate"] == "slower_than_population_prior"
    assert packet.personalization["stress_reactivity"] == "faster_than_population_prior"
    assert packet.personalization["population_prior"]["workload_sensitivity_i"]["mean"] == 2.9
    assert "high_personal_workload_sensitivity" in {
        item["code"] for item in packet.reason_candidates
    }


def test_late_day_load_does_not_explain_morning_warning():
    packet = _packet(
        [],
        output={
            "trajectory": [
                {"time": "10:00", "stress_0_10": 5.0, "continuous_load_factor": 0.10},
                {"time": "10:30", "stress_0_10": 5.2, "continuous_load_factor": 0.15},
                {"time": "18:00", "stress_0_10": 8.0, "continuous_load_factor": 0.95},
            ]
        },
    )

    assert packet.trajectory["local_continuous_load_factor"] == 0.15
    assert packet.trajectory["day_continuous_load_factor"] == 0.95
    assert "sustained_continuous_load" not in {
        item["code"] for item in packet.reason_candidates
    }


def test_morning_local_load_explains_morning_warning():
    packet = _packet(
        [],
        output={
            "trajectory": [
                {"time": "09:30", "stress_0_10": 5.0, "continuous_load_factor": 0.70},
                {"time": "10:30", "stress_0_10": 6.2, "continuous_load_factor": 0.80},
                {"time": "18:00", "stress_0_10": 8.0, "continuous_load_factor": 0.10},
            ]
        },
    )

    assert packet.trajectory["local_continuous_load_factor"] == 0.8
    assert "sustained_continuous_load" in {
        item["code"] for item in packet.reason_candidates
    }


def test_risk_trajectory_uses_local_slope_and_keeps_day_peak_as_background():
    packet = _packet(
        [],
        output={
            "trajectory": [
                {"time": "09:20", "stress_0_10": 4.0},
                {"time": "10:30", "stress_0_10": 6.0},
                {"time": "18:00", "stress_0_10": 9.0},
            ]
        },
    )

    assert packet.trajectory["trajectory"] == "rising"
    assert packet.trajectory["local_peak_time"] == "10:30"
    assert packet.trajectory["day_peak_time"] == "18:00"
