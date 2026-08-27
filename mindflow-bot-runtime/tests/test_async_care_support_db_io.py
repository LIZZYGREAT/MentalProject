import asyncio
import threading
import time
import uuid

from app.agent.context import AgentContext
from app.tools.care import CareTools


class _SlowObservations:
    def __init__(self):
        self.thread_id = None

    def recent_before(self, *_args, **_kwargs):
        self.thread_id = threading.get_ident()
        time.sleep(0.30)
        return []


class _SlowProfiles:
    def __init__(self):
        self.thread_id = None

    def current(self, _participant_id):
        self.thread_id = threading.get_ident()
        time.sleep(0.30)
        return None


class _SlowPreferences:
    def __init__(self):
        self.thread_id = None

    def get(self, _participant_id):
        self.thread_id = threading.get_ident()
        time.sleep(0.30)
        return {"version": 1, "allow_follow_up": True}


class _Coordinator:
    async def ensure_forecast(self, *_args, **_kwargs):
        return {
            "id": str(uuid.uuid4()),
            "forecast_version": "support-forecast-v1",
            "output": {"alerts": []},
            "calendar_events": [],
            "calendar_degraded": True,
        }


def test_care_get_support_repository_reads_do_not_block_event_loop():
    observations = _SlowObservations()
    profiles = _SlowProfiles()
    preferences = _SlowPreferences()
    tools = CareTools(
        profiles,
        observations,
        None,
        None,
        "Asia/Shanghai",
        _Coordinator(),
        care_preferences=preferences,
    )
    ctx = AgentContext(
        uuid.uuid4(), "P", "ou", "oc", "support-message", uuid.uuid4()
    )

    async def scenario():
        loop_thread = threading.get_ident()
        heartbeat = asyncio.Event()

        async def pulse():
            await asyncio.sleep(0.05)
            heartbeat.set()

        support_task = asyncio.create_task(tools.get_support(ctx, {}))
        pulse_task = asyncio.create_task(pulse())
        await asyncio.wait_for(heartbeat.wait(), timeout=0.15)
        # The three repository reads each sleep for 300 ms. Reaching this
        # assertion while support is still running proves the loop remained
        # schedulable instead of merely checking total wall-clock duration.
        assert support_task.done() is False
        result = await asyncio.wait_for(support_task, timeout=0.60)
        await pulse_task
        assert result["ok"] is True
        assert observations.thread_id != loop_thread
        assert profiles.thread_id != loop_thread
        assert preferences.thread_id != loop_thread

    asyncio.run(scenario())
