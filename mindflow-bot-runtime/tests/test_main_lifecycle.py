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
