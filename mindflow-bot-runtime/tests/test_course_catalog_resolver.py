from services.course_catalog import CourseCatalogResolver
from services.event_lifecycle import prepare_event_instances


def _event(summary: str, **values):
    return {
        "id": f"event-{summary}",
        "summary": summary,
        "start_time": "2030-01-15T09:00:00+08:00",
        "end_time": "2030-01-15T10:30:00+08:00",
        **values,
    }


def test_high_math_alias_is_a_course_with_bounded_catalog_candidates():
    resolver = CourseCatalogResolver()
    resolution = resolver.resolve("高数")

    assert resolution.likely_course is True
    assert 1 <= len(resolution.candidates) <= 5
    assert all("高等数学" in item.canonical_name for item in resolution.candidates[:3])
    event = prepare_event_instances([_event("高数")], "2030-01-15")[0]
    assert event["event_type"] == "course"


def test_explicitly_empty_catalog_does_not_fall_back_to_generated_catalog():
    resolution = CourseCatalogResolver(catalog={}, aliases={}).resolve("高数")

    assert resolution.candidates == ()
    assert resolution.exact_match is None


def test_high_math_a_ranks_a_class_first_without_claiming_local_exact_identity():
    resolution = CourseCatalogResolver().resolve("高数A")

    assert resolution.candidates[0].canonical_name == "高等数学（A类）II"
    assert resolution.candidates[0].code == "AMTD0034"
    assert resolution.exact_match is None


def test_linear_algebra_alias_resolves_exact_canonical_identity():
    event = prepare_event_instances([_event("线代")], "2030-01-15")[0]

    assert event["event_type"] == "course"
    assert event["course_name"] == "线性代数"
    assert event["course_code"] == "SCIE0038"
    assert event["course_match_source"] == "catalog_alias"


def test_course_related_homework_remains_task_with_related_course():
    event = prepare_event_instances([_event("写高数作业")], "2030-01-15")[0]

    assert event["event_type"] == "task"
    assert event["task_type"] == "homework"
    assert event["related_course_name"].startswith("高等数学")
    assert not event.get("course_name")


def test_course_exam_remains_exam_with_related_course():
    event = prepare_event_instances([_event("高数期末考试")], "2030-01-15")[0]

    assert event["event_type"] == "task"
    assert event["task_type"] == "exam"
    assert event["related_course_name"].startswith("高等数学")
