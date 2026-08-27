from services.course_catalog import CourseCatalogResolver
from services.event_classifier import finalize_event_classification
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


def test_ambiguous_course_related_homework_defers_canonical_identity():
    event = prepare_event_instances([_event("写高数作业")], "2030-01-15")[0]

    assert event["event_type"] == "task"
    assert event["task_type"] == "homework"
    assert not event.get("related_course_name")
    assert event["metadata"]["classification"]["course_catalog_context"]["candidates"]
    assert not event.get("course_name")


def test_ambiguous_course_exam_defers_canonical_identity():
    event = prepare_event_instances([_event("高数期末考试")], "2030-01-15")[0]

    assert event["event_type"] == "task"
    assert event["task_type"] == "exam"
    assert not event.get("related_course_name")
    assert event["metadata"]["classification"]["course_catalog_context"]["candidates"]


def test_fuzzy_catalog_candidate_cannot_authoritatively_create_course_event():
    resolution = CourseCatalogResolver().resolve("提高数学能力")
    event = prepare_event_instances(
        [_event("提高数学能力")], "2030-01-15"
    )[0]

    assert resolution.alias_match_kind == "retrieval_only"
    assert resolution.strong_course_evidence is False
    assert event["event_type"] == "task"
    assert not event.get("course_name")
    assert not event.get("related_course_name")


def test_title_course_identity_wins_over_task_language_only_in_description():
    event = prepare_event_instances(
        [_event("高数", description="课后完成作业")], "2030-01-15"
    )[0]

    assert event["event_type"] == "course"
    assert event["task_type"] == "course"


def test_title_task_intent_wins_even_when_description_calls_it_a_course():
    event = prepare_event_instances(
        [_event("写高数作业", description="高数课程相关")], "2030-01-15"
    )[0]

    assert event["event_type"] == "task"
    assert event["task_type"] == "homework"
    assert not event.get("related_course_name")


def test_exact_course_related_homework_keeps_canonical_identity():
    event = prepare_event_instances([_event("写线性代数作业")], "2030-01-15")[0]

    assert event["event_type"] == "task"
    assert event["task_type"] == "homework"
    assert event["related_course_name"] == "线性代数"
    assert event["related_course_code"] == "SCIE0038"
    assert event["metadata"]["classification"]["course_identity_locked"] is True


def test_high_math_a_variant_rejects_b_candidate():
    event = prepare_event_instances([_event("高数A")], "2030-01-15")[0]
    finalized = finalize_event_classification(
        event,
        external_course_match={
            "matched": True,
            "canonical_name": "高等数学（B类）II",
            "code": "AMTD0035",
            "confidence": 0.99,
        },
    )

    assert event["metadata"]["classification"]["event_type_locked"] is True
    assert event["metadata"]["classification"]["course_identity_locked"] is False
    context = event["metadata"]["classification"]["course_catalog_context"]
    assert context["identity_constraints"] == {"variant": "A"}
    assert context["candidates"]
    assert all("A类" in item["canonical_name"] for item in context["candidates"])
    assert not finalized.get("course_name")
    assert not finalized.get("course_code")


def test_high_math_a_variant_accepts_a_candidate():
    event = prepare_event_instances([_event("高数A")], "2030-01-15")[0]
    candidate = event["metadata"]["classification"]["course_catalog_context"][
        "candidates"
    ][0]
    finalized = finalize_event_classification(
        event,
        external_course_match={
            "matched": True,
            "canonical_name": candidate["canonical_name"],
            "code": candidate["code"],
            "confidence": 0.99,
        },
    )

    assert "A类" in finalized["course_name"]
    assert finalized["course_code"] == candidate["code"]


def test_high_math_b_variant_rejects_a_candidate():
    event = prepare_event_instances([_event("高数B")], "2030-01-15")[0]
    context = event["metadata"]["classification"]["course_catalog_context"]
    finalized = finalize_event_classification(
        event,
        external_course_match={
            "matched": True,
            "canonical_name": "高等数学（A类）II",
            "code": "AMTD0034",
            "confidence": 0.99,
        },
    )

    assert context["identity_constraints"] == {"variant": "B"}
    assert context["candidates"]
    assert all("B类" in item["canonical_name"] for item in context["candidates"])
    assert not finalized.get("course_name")


def test_unconstrained_high_math_keeps_multiple_variants():
    event = prepare_event_instances([_event("高数")], "2030-01-15")[0]
    context = event["metadata"]["classification"]["course_catalog_context"]
    names = {item["canonical_name"] for item in context["candidates"]}

    assert context["identity_constraints"] == {}
    assert any("A类" in name for name in names)
    assert any("B类" in name for name in names)


def test_ambiguous_high_math_suffixes_never_lock_a_wrong_identity():
    for title in ("高数", "高数I", "高数II", "高数1", "高数2", "高数3"):
        event = prepare_event_instances([_event(title)], "2030-01-15")[0]
        classification = event["metadata"]["classification"]
        assert event["event_type"] == "course"
        assert classification["event_type_locked"] is True
        assert classification["course_identity_locked"] is False
        assert not event.get("course_name")


def test_exact_course_identity_is_independently_locked():
    event = prepare_event_instances([_event("线代")], "2030-01-15")[0]
    classification = event["metadata"]["classification"]
    assert classification["event_type_locked"] is True
    assert classification["course_identity_locked"] is True


def test_non_course_negative_corpus_has_zero_authoritative_course_false_positives():
    seed_titles = (
        "看电影", "中国", "管理", "经济", "文化", "政治", "数学", "英语",
        "提高数学能力", "买菜", "吃晚饭", "晨跑", "健身", "洗衣服", "取快递",
        "朋友聚会", "家庭电话", "团队沟通", "整理房间", "阅读新闻", "预算复盘",
        "旅行规划", "医院预约", "银行办事", "打印材料", "设备维修", "看展览",
        "听音乐", "午休", "散步", "喝水", "准备早餐", "周末采购", "缴纳账单",
        "更新密码", "备份照片", "清理邮箱", "回复消息", "项目讨论", "工作周会",
    )
    generic_subjects = (
        "生活安排", "团队进度", "个人预算", "旅行计划", "健康状态", "饮食习惯",
        "运动目标", "家庭事务", "工作流程", "设备情况", "新闻内容", "电影清单",
        "音乐收藏", "房间收纳", "购物需求", "交通路线", "沟通方式", "时间管理",
    )
    generated_titles = tuple(
        f"{verb}{subject}"
        for verb in ("讨论", "整理", "了解", "提高", "规划")
        for subject in generic_subjects
    )
    titles = seed_titles + generated_titles
    rows = prepare_event_instances(
        [_event(title) for title in titles], "2030-01-15"
    )
    false_positives = [
        title
        for title, row in zip(titles, rows)
        if row["event_type"] == "course"
    ]

    assert len(set(titles)) >= 100
    assert false_positives == []
