"""Run a database-free smoke check against DeepSeek Native Web Search."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from app.config import Settings
from app.services.web_search_service import (
    DeepSeekNativeSearchProvider,
    SearchUnavailable,
)


PUBLIC_QUERY = "DeepSeek latest model release"


def _provider(settings: Settings) -> DeepSeekNativeSearchProvider:
    if not settings.web_search_enabled:
        raise SearchUnavailable("provider_not_configured")
    if settings.web_search_provider != "deepseek_native":
        raise SearchUnavailable("provider_not_configured")
    return DeepSeekNativeSearchProvider(
        base_url=settings.claude_anthropic_base_url,
        api_key=settings.deepseek_api_key,
        model=settings.web_search_model,
        timeout_seconds=settings.web_search_timeout_seconds,
        max_uses=settings.web_search_max_uses,
        max_output_tokens=settings.web_search_max_output_tokens,
    )


async def run(provider: DeepSeekNativeSearchProvider) -> int:
    started = time.monotonic()
    try:
        result = await provider.search(PUBLIC_QUERY, "month", 5)
    except SearchUnavailable as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        print(
            f"FAIL provider={provider.provider_name} "
            f"reason_code={str(exc) or 'provider_unavailable'} "
            f"latency_ms={latency_ms}"
        )
        return 1
    except Exception:
        latency_ms = int((time.monotonic() - started) * 1000)
        print(
            f"FAIL provider={provider.provider_name} "
            f"reason_code=provider_unavailable latency_ms={latency_ms}"
        )
        return 1

    latency_ms = int((time.monotonic() - started) * 1000)
    print(
        f"PASS provider={result.provider} sources={len(result.sources)} "
        f"request_id={result.request_id or '-'} latency_ms={latency_ms}"
    )
    for source in result.sources:
        title = " ".join(source.title.split())[:160]
        domain = urlsplit(source.url).hostname or "unknown"
        print(f"SOURCE title={title} domain={domain}")
    return 0


def main() -> int:
    try:
        settings = Settings.from_env(base_dir=RUNTIME_ROOT)
        provider = _provider(settings)
    except (SearchUnavailable, ValueError) as exc:
        reason = str(exc) if isinstance(exc, SearchUnavailable) else "invalid_config"
        print(f"FAIL provider=deepseek_native reason_code={reason}")
        return 1
    return asyncio.run(run(provider))


if __name__ == "__main__":
    raise SystemExit(main())
