import threading

import pytest

from app.smoke.feishu_gateway import run_smoke


class SmokeStopErrorChannel:
    def __init__(self, **_kwargs):
        self.is_ready = False
        self._stopped = threading.Event()

    def on(self, name, _handler):
        assert name == "message"

    def start(self):
        self.is_ready = True
        self._stopped.wait(5.0)

    def stop(self):
        self._stopped.set()
        raise RuntimeError("SMOKE_STOP_BROKEN")


def test_smoke_rejects_receiver_shutdown_error(capsys):
    with pytest.raises(
        SystemExit,
        match="failed during shutdown: RuntimeError: SMOKE_STOP_BROKEN",
    ):
        run_smoke(
            "cli_test",
            "secret",
            seconds=0,
            start_timeout=5,
            stop_timeout=3,
            channel_factory=SmokeStopErrorChannel,
        )
    assert "disconnected" not in capsys.readouterr().out
