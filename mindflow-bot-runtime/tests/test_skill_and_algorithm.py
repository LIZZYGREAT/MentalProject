from app.agent.skill_loader import SkillLoader
from mindflow_core.assessment import AssessmentModel
from helpers import skill_path


def test_skill_frontmatter_is_parsed_and_stable():
    loader = SkillLoader(skill_path())
    first = loader.load()
    second = loader.current()
    assert first.metadata["name"] == "mental-health-care"
    assert "care_record_checkin" in first.instructions
    assert first.version == second.version


def test_existing_algorithm_runs_through_structured_adapter():
    result = AssessmentModel().predict(
        profile={},
        observations=[
            {
                "type": "checkin",
                "observed_at": "2026-08-09T08:00:00+08:00",
                "payload": {"stress_0_10": 4, "energy_0_10": 7},
            }
        ],
        calendar_events=[],
        local_date="2026-08-09",
        calendar_degraded=True,
    )
    assert 0 <= result.stress_0_10 <= 10
    assert 0 <= result.vitality_0_10 <= 10
    assert result.point_count > 0
    assert result.calendar_degraded is True
