"""Run a real CardKit streaming smoke against one explicit Feishu chat."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from app.config import Settings
from app.integrations.feishu.client import FeishuClient
from app.integrations.feishu.streaming_card import (
    ANSWER_ELEMENT_ID,
    streaming_answer_card,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chat-id",
        default=os.getenv("FEISHU_CARDKIT_SMOKE_CHAT_ID", ""),
        help="Target chat_id (or set FEISHU_CARDKIT_SMOKE_CHAT_ID).",
    )
    return parser.parse_args()


def _failure(exc: BaseException, *, stage: str, started: float) -> int:
    print(
        "FAIL "
        f"stage={stage} "
        f"provider_code={getattr(exc, 'code', None) or '-'} "
        f"request_id={getattr(exc, 'provider_request_id', None) or '-'} "
        f"latency_ms={int((time.monotonic() - started) * 1000)}"
    )
    return 1


def main() -> int:
    args = _arguments()
    chat_id = str(args.chat_id or "").strip()
    if not chat_id:
        print("FAIL stage=config provider_code=- request_id=- latency_ms=0")
        return 2
    started = time.monotonic()
    try:
        settings = Settings.from_env(base_dir=RUNTIME_ROOT)
        client = FeishuClient(
            settings.feishu_bot_app_id,
            settings.feishu_bot_app_secret,
        )
    except Exception as exc:
        return _failure(exc, stage="config", started=started)

    try:
        card_id = client.create_card_instance(streaming_answer_card())
    except Exception as exc:
        return _failure(exc, stage="create_card", started=started)
    try:
        client.send_card_by_reference(chat_id, card_id)
    except Exception as exc:
        return _failure(exc, stage="send_reference", started=started)
    try:
        client.update_card_element_content(
            card_id, ANSWER_ELEMENT_ID, "CardKit smoke", 1
        )
        client.update_card_element_content(
            card_id, ANSWER_ELEMENT_ID, "CardKit smoke PASS", 2
        )
    except Exception as exc:
        return _failure(exc, stage="update", started=started)
    try:
        client.finish_streaming_card(card_id, 3)
    except Exception as exc:
        return _failure(exc, stage="finalize", started=started)

    print(
        "PASS stage=finalize provider_code=0 request_id=- "
        f"latency_ms={int((time.monotonic() - started) * 1000)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
