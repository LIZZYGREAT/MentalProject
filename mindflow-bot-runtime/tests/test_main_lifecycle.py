import asyncio

from app import main as app_main


def test_sigterm_during_gateway_start_still_cancels_start(monkeypatch):
    class SlowGateway:
        def __init__(self):
            self.start_cancelled = False

        async def start(self):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.start_cancelled = True
                raise

    gateway = SlowGateway()

    async def scenario():
        loop = asyncio.get_running_loop()

        def trigger(_sig, callback):
            loop.call_soon(callback)

        monkeypatch.setattr(loop, "add_signal_handler", trigger)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: None)
        await asyncio.wait_for(
            app_main._run_gateway_until_shutdown(gateway), timeout=1
        )
        assert gateway.start_cancelled

    asyncio.run(scenario())


def test_scheduler_ready_callback_runs_only_after_gateway_start(monkeypatch):
    order = []

    class Gateway:
        async def start(self):
            order.append("gateway_ready")

        async def wait_closed(self):
            order.append("gateway_wait")

    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda *_args: None)

        async def start_scheduler():
            order.append("scheduler_ready")

        await app_main._run_gateway_until_shutdown(
            Gateway(), on_ready=start_scheduler
        )

    asyncio.run(scenario())
    assert order == ["gateway_ready", "scheduler_ready", "gateway_wait"]
