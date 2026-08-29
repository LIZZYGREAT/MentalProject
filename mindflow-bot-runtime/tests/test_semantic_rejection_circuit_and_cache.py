import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
import requests
from sqlalchemy import func, select

from app.db import Database, build_engine
from app.models import EventSemanticCache
from app.repositories import EventSemanticCacheRepository, ParticipantRepository
from app.services.event_semantic_preprocessor import EventSemanticPreprocessor
from services.event_semantic_prompt import PROMPT_VERSION
from services.event_semantics import DIMENSIONS
from tests.helpers import memory_database


def _event(event_id: str, summary: str) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "description": "需要完成的任务",
        "event_type": "task",
        "task_type": "general",
        "start_time": "2030-01-15T09:00:00+08:00",
        "end_time": "2030-01-15T10:00:00+08:00",
    }


def _response(confidence: float) -> dict:
    return {
        **{dimension: 0.5 for dimension in DIMENSIONS},
        "appraisal_score_1_10": 5.0,
        "confidence": confidence,
        "evidence_tags": ["task"],
        "reasoning_summary": "任务语义",
        "event_classification": {
            "event_type": "task",
            "task_type": "general",
            "confidence": confidence,
        },
        "course_match": {"matched": False},
    }


class SequencedClient:
    provider = "test-provider"
    model = "semantic-test"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def infer(self, event):
        self.calls.append(event["summary"])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _preprocessor(client):
    database = memory_database()
    participant = ParticipantRepository(database).create("SEMANTIC-REJECTION")
    cache = EventSemanticCacheRepository(database)
    return (
        database,
        participant,
        cache,
        EventSemanticPreprocessor(
            cache,
            client=client,
            model="semantic-test",
            max_concurrency=1,
            circuit_seconds=60,
        ),
    )


def test_sqlite_first_insert_race_upserts_one_semantic_cache_row(tmp_path):
    database = Database(
        build_engine(f"sqlite:///{(tmp_path / 'semantic-cache.db').as_posix()}")
    )
    database.create_schema_for_tests()
    participant = ParticipantRepository(database).create("SEMANTIC-UPSERT-RACE")
    barrier = threading.Barrier(2)

    def write(status):
        cache = EventSemanticCacheRepository(database)
        barrier.wait(timeout=2)
        if status == "complete":
            cache.put_complete(
                participant.id,
                "f" * 64,
                {"writer": status},
                schema_version="event_semantics.v3",
                prompt_version=PROMPT_VERSION,
                model="semantic-test",
            )
        else:
            cache.put_partial(
                participant.id,
                "f" * 64,
                {"writer": status},
                schema_version="event_semantics.v3",
                prompt_version=PROMPT_VERSION,
                model="semantic-test",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, status) for status in ("complete", "partial")]
        for future in futures:
            future.result(timeout=5)

    with database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(EventSemanticCache)
        ) == 1
    entry = EventSemanticCacheRepository(database).get_entry(
        participant.id,
        "f" * 64,
        schema_version="event_semantics.v3",
        prompt_version=PROMPT_VERSION,
        model="semantic-test",
    )
    assert entry["status"] == "complete"
    assert entry["assessment"]["writer"] == entry["status"]


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("complete", "rejected", "complete"),
        ("complete", "partial", "complete"),
        ("partial", "rejected", "partial"),
        ("rejected", "complete", "complete"),
    ],
)
def test_semantic_cache_quality_never_downgrades(first, second, expected):
    database = memory_database()
    participant = ParticipantRepository(database).create(
        f"SEMANTIC-PRECEDENCE-{first}-{second}"
    )
    cache = EventSemanticCacheRepository(database)
    fingerprint = "a" * 64

    def write(status):
        if status == "complete":
            cache.put_complete(
                participant.id, fingerprint, {"writer": status},
                schema_version="event_semantics.v3",
                prompt_version=PROMPT_VERSION,
                model="semantic-test",
            )
        elif status == "partial":
            cache.put_partial(
                participant.id, fingerprint, {"writer": status},
                schema_version="event_semantics.v3",
                prompt_version=PROMPT_VERSION,
                model="semantic-test",
            )
        else:
            cache.put_rejected(
                participant.id, fingerprint,
                reason="test_rejection", confidence=0.1,
                assessment={"writer": status},
                schema_version="event_semantics.v3",
                prompt_version=PROMPT_VERSION,
                model="semantic-test",
            )

    write(first)
    write(second)
    entry = cache.get_entry(
        participant.id, fingerprint,
        schema_version="event_semantics.v3",
        prompt_version=PROMPT_VERSION,
        model="semantic-test",
    )
    assert entry["status"] == expected
    assert entry["assessment"]["writer"] == expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("complete", "rejected", "complete"),
        ("complete", "partial", "complete"),
        ("partial", "rejected", "partial"),
        ("rejected", "complete", "complete"),
    ],
)
def test_sqlite_semantic_cache_concurrent_quality_is_monotonic(
    tmp_path, left, right, expected
):
    database = Database(
        build_engine(
            f"sqlite:///{(tmp_path / f'semantic-{left}-{right}.db').as_posix()}"
        )
    )
    database.create_schema_for_tests()
    participant = ParticipantRepository(database).create(
        f"SEMANTIC-RACE-{left}-{right}"
    )
    barrier = threading.Barrier(2)
    fingerprint = "b" * 64

    def write(status):
        cache = EventSemanticCacheRepository(database)
        barrier.wait(timeout=2)
        if status == "complete":
            cache.put_complete(
                participant.id, fingerprint, {"writer": status},
                schema_version="event_semantics.v3",
                prompt_version=PROMPT_VERSION, model="semantic-test",
            )
        elif status == "partial":
            cache.put_partial(
                participant.id, fingerprint, {"writer": status},
                schema_version="event_semantics.v3",
                prompt_version=PROMPT_VERSION, model="semantic-test",
            )
        else:
            cache.put_rejected(
                participant.id, fingerprint,
                reason="race_rejection", confidence=0.1,
                assessment={"writer": status},
                schema_version="event_semantics.v3",
                prompt_version=PROMPT_VERSION, model="semantic-test",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, status) for status in (left, right)]
        for future in futures:
            future.result(timeout=5)

    entry = EventSemanticCacheRepository(database).get_entry(
        participant.id, fingerprint,
        schema_version="event_semantics.v3",
        prompt_version=PROMPT_VERSION, model="semantic-test",
    )
    assert entry["status"] == expected
    assert entry["assessment"]["writer"] == expected


def test_low_confidence_is_negative_cached_without_blocking_next_event():
    async def scenario():
        client = SequencedClient([_response(0.20), _response(0.90)])
        _database, participant, cache, preprocessor = _preprocessor(client)
        low = _event("low", "含义模糊的事情")
        good = _event("good", "准备课程作业")

        _, _, _, low_misses = preprocessor.prepare(
            participant.id, [low], consent=True
        )
        assert len(low_misses) == 1
        assert await preprocessor._enrich_one(participant.id, low_misses[0]) is False
        assert preprocessor._circuit_until <= time.monotonic()

        rejected = cache.get_entry(
            participant.id,
            low_misses[0]["fingerprint"],
            schema_version="event_semantics.v4",
            prompt_version=PROMPT_VERSION,
            model="semantic-test",
        )
        assert rejected["status"] == "rejected"
        assert rejected["assessment"]["rejection"] == {
            "reason": "low_confidence",
            "confidence": 0.2,
        }

        _, _, _, good_misses = preprocessor.prepare(
            participant.id, [good], consent=True
        )
        assert len(good_misses) == 1
        assert await preprocessor._enrich_one(participant.id, good_misses[0]) is True
        assert client.calls == ["含义模糊的事情", "准备课程作业"]

        prepared, _, status, repeated_misses = preprocessor.prepare(
            participant.id, [low, good], consent=True
        )
        assert repeated_misses == []
        assert [item["metadata"]["semantic"]["source"] for item in prepared] == [
            "rules",
            "hybrid",
        ]
        assert status == "hybrid_partial"
        assert client.calls == ["含义模糊的事情", "准备课程作业"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [requests.Timeout("timeout"), requests.HTTPError("429 too many requests")],
)
def test_provider_availability_failures_open_circuit(failure):
    async def scenario():
        client = SequencedClient([failure])
        _database, participant, _cache, preprocessor = _preprocessor(client)
        _, _, _, misses = preprocessor.prepare(
            participant.id, [_event("provider", "provider failure")], consent=True
        )
        before = time.monotonic()
        assert await preprocessor._enrich_one(participant.id, misses[0]) is False
        assert preprocessor._circuit_until >= before + 59

    asyncio.run(scenario())


def test_malformed_provider_payload_opens_circuit_but_plain_value_error_does_not():
    async def scenario():
        malformed = SequencedClient([{}])
        _database, participant, _cache, preprocessor = _preprocessor(malformed)
        _, _, _, misses = preprocessor.prepare(
            participant.id, [_event("bad-json", "malformed")], consent=True
        )
        assert await preprocessor._enrich_one(participant.id, misses[0]) is False
        assert preprocessor._circuit_until > time.monotonic()

        programming_error = SequencedClient([ValueError("local validation bug")])
        _database, participant, _cache, preprocessor = _preprocessor(programming_error)
        _, _, _, misses = preprocessor.prepare(
            participant.id, [_event("value-error", "local error")], consent=True
        )
        assert await preprocessor._enrich_one(participant.id, misses[0]) is False
        assert preprocessor._circuit_until <= time.monotonic()

    asyncio.run(scenario())
