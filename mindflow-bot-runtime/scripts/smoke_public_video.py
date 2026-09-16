"""Run a bounded, anonymous smoke check for a public Bilibili video URL."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
from urllib.parse import urlsplit


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from app.contracts.public_video import VideoProviderError
from app.services.public_web_document_service import PublicWebDocumentService
from app.services.video_providers.bilibili import BilibiliVideoAdapter


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check anonymous public Bilibili metadata and subtitle access."
    )
    parser.add_argument("--url", required=True, help="Public HTTPS Bilibili video URL")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-bytes", type=int, default=512 * 1024)
    return parser.parse_args()


def _cid(adapter: BilibiliVideoAdapter, video_id: str, canonical_url: str) -> str:
    data = adapter._metadata_payloads.get(video_id, {})
    page_number = adapter._part_number(canonical_url)
    page = adapter._select_page(data, page_number)
    return str(adapter._page_cid(page) or data.get("cid") or "-")


async def _run(url: str, *, timeout: float, max_bytes: int) -> int:
    transport = PublicWebDocumentService(
        repository=None,
        timeout_seconds=timeout,
        max_bytes=max_bytes,
    )
    adapter = BilibiliVideoAdapter(
        transport,
        api_max_bytes=max_bytes,
        timeout_seconds=timeout,
    )
    try:
        metadata = await adapter.resolve_metadata(url)
    except VideoProviderError as exc:
        print(f"metadata FAIL reason_code={exc.reason_code}")
        return 1
    except Exception:
        print("metadata FAIL reason_code=provider_unavailable")
        return 1

    host = urlsplit(metadata.canonical_url).hostname or "-"
    print(
        f"metadata PASS provider={metadata.provider} video_id={metadata.video_id} "
        f"cid={_cid(adapter, metadata.video_id, metadata.canonical_url)} host={host}"
    )
    try:
        transcript = await adapter.resolve_transcript(metadata)
    except VideoProviderError as exc:
        if exc.reason_code == "no_public_subtitle":
            print(
                "transcript PASS transcript_available=false "
                "transcript_chars=0 first_chunk_chars=0"
            )
            return 0
        print(f"transcript FAIL reason_code={exc.reason_code}")
        return 1
    except Exception:
        print("transcript FAIL reason_code=provider_unavailable")
        return 1

    text = transcript.text
    print(
        f"transcript PASS transcript_available={bool(transcript.segments)} "
        f"transcript_chars={len(text)} first_chunk_chars={min(len(text), 8000)}"
    )
    return 0


def main() -> int:
    args = _arguments()
    return asyncio.run(_run(args.url, timeout=args.timeout, max_bytes=args.max_bytes))


if __name__ == "__main__":
    raise SystemExit(main())
