from datetime import datetime, timedelta, timezone

from app.repositories_course_schedule_image import (
    CourseScheduleImageSessionRepository,
)
from helpers import memory_database, participant


def test_image_session_survives_repository_recreation_and_keeps_failure_report():
    database = memory_database()
    owner = participant(database, "IMAGE-SESSION-PERSIST")
    first_repository = CourseScheduleImageSessionRepository(database)
    first_repository.start(
        owner.id,
        chat_id="chat",
        image_message_id="image-message",
        image_key="image-key",
        vision_model="vision-model",
    )
    first_repository.mark_failed(
        owner.id,
        "image-message",
        error_code="schedule_validation_failed",
        error_detail="course_name is missing",
        parse_report={"quarantined": [{"path": "courses[1]"}]},
    )

    restored = CourseScheduleImageSessionRepository(database).last_failure(
        owner.id, chat_id="chat"
    )

    assert restored["status"] == "needs_retry"
    assert restored["last_error_code"] == "schedule_validation_failed"
    assert restored["parse_report"]["quarantined"][0]["path"] == "courses[1]"
    assert restored["image_key"] == "image-key"


def test_expired_image_session_is_archived_after_twenty_four_hours():
    database = memory_database()
    owner = participant(database, "IMAGE-SESSION-EXPIRE")
    repository = CourseScheduleImageSessionRepository(database, ttl_hours=24)
    started_at = datetime(2026, 9, 8, tzinfo=timezone.utc)
    repository.start(
        owner.id,
        chat_id="chat",
        image_message_id="old-image",
        image_key="old-key",
        now=started_at,
    )

    active = repository.latest(
        owner.id, chat_id="chat", now=started_at + timedelta(hours=25)
    )
    archived = repository.latest(
        owner.id,
        chat_id="chat",
        include_archived=True,
        now=started_at + timedelta(hours=25),
    )

    assert active is None
    assert archived["status"] == "archived"
