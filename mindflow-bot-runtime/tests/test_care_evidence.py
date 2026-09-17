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


def _packet(events, *, output=None, profile=None, preferences=None):
    return CareEvidenceBuilder("Asia/Shanghai").build(
        source="forecast_warning",
        local_date=TARGET,
        alert={"time": "10:40", "S": 8.1, "V": 4.6, "F": 0.63},
        forecast_output=output,
        calendar_events=events,
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
