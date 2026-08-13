"""Connect the isolated Feishu receiver briefly without exposing credentials."""

from __future__ import annotations

import argparse
import multiprocessing
import os
from queue import Empty
import time

from app.integrations.feishu.receiver_process import receiver_process_main


def _bot_credentials(environment: dict[str, str]) -> tuple[str, str]:
    return (
        environment.get("FEISHU_BOT_APP_ID", "").strip()
        or environment.get("FEISHU_APP_ID", "").strip(),
        environment.get("FEISHU_BOT_APP_SECRET", "").strip()
        or environment.get("FEISHU_APP_SECRET", "").strip(),
    )


def _failure(stage: str, envelope: dict) -> SystemExit:
    error_type = str(envelope.get("error_type") or "ReceiverError")
    message = str(envelope.get("message") or "unknown receiver failure")
    return SystemExit(
        f"Feishu gateway smoke failed during {stage}: {error_type}: {message}"
    )


def run_smoke(
    app_id: str,
    app_secret: str,
    *,
    seconds: float,
    start_timeout: float,
    stop_timeout: float = 8.0,
    channel_factory=None,
) -> None:
    """Require ready, error-free operation, clean stopped, and exit code zero."""

    context = multiprocessing.get_context("spawn")
    output_queue = context.Queue()
    stop_event = context.Event()
    process = context.Process(
        name="feishu-ws-smoke-receiver",
        target=receiver_process_main,
        args=(app_id, app_secret, output_queue, stop_event, channel_factory),
        daemon=True,
    )
    ready = False
    stopped = False
    process.start()
    try:
        deadline = time.monotonic() + start_timeout
        while time.monotonic() < deadline:
            try:
                envelope = output_queue.get(timeout=0.2)
            except Empty:
                if not process.is_alive():
                    raise SystemExit(
                        f"Feishu receiver exited before ready (code={process.exitcode})"
                    )
                continue
            if envelope.get("kind") == "ready":
                ready = True
                print("feishu_gateway_smoke ready", flush=True)
                break
            if envelope.get("kind") == "error":
                raise _failure("startup", envelope)
        if not ready:
            raise SystemExit("Feishu receiver readiness timed out")

        alive_until = time.monotonic() + seconds
        while time.monotonic() < alive_until:
            try:
                envelope = output_queue.get(
                    timeout=min(0.2, max(0.0, alive_until - time.monotonic()))
                )
            except Empty:
                if not process.is_alive():
                    raise SystemExit(
                        f"Feishu receiver exited during smoke (code={process.exitcode})"
                    )
                continue
            if envelope.get("kind") == "error":
                raise _failure("connected", envelope)
            if envelope.get("kind") == "stopped":
                raise SystemExit("Feishu receiver stopped before shutdown was requested")

        stop_event.set()
        shutdown_deadline = time.monotonic() + stop_timeout
        while time.monotonic() < shutdown_deadline:
            try:
                envelope = output_queue.get(timeout=0.2)
            except Empty:
                continue
            if envelope.get("kind") == "error":
                raise _failure("shutdown", envelope)
            if envelope.get("kind") == "stopped":
                stopped = True
                break
        if not stopped:
            raise SystemExit(
                "Feishu gateway smoke failed during shutdown: terminal state timed out"
            )
        process.join(timeout=max(0.0, shutdown_deadline - time.monotonic()))
        if process.is_alive():
            raise SystemExit(
                "Feishu gateway smoke failed during shutdown: receiver did not exit"
            )
        if process.exitcode != 0:
            raise SystemExit(f"Feishu receiver stopped with code={process.exitcode}")
    finally:
        stop_event.set()
        process.join(timeout=stop_timeout)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
        output_queue.close()
        output_queue.join_thread()
    print("feishu_gateway_smoke disconnected", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--start-timeout", type=float, default=30.0)
    parser.add_argument("--stop-timeout", type=float, default=8.0)
    args = parser.parse_args()
    if args.seconds < 0 or args.start_timeout <= 0 or args.stop_timeout <= 0:
        parser.error("durations must be positive")

    app_id, app_secret = _bot_credentials(os.environ)
    if not app_id or not app_secret:
        raise SystemExit(
            "FEISHU_BOT_APP_ID and FEISHU_BOT_APP_SECRET must be configured"
        )
    run_smoke(
        app_id,
        app_secret,
        seconds=args.seconds,
        start_timeout=args.start_timeout,
        stop_timeout=args.stop_timeout,
    )


if __name__ == "__main__":
    main()
