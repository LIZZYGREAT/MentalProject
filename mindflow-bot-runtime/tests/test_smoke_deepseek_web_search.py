import asyncio

from app.services.web_search_service import (
    SearchProviderResult,
    SearchSource,
    SearchUnavailable,
)
from scripts import smoke_deepseek_web_search as smoke


class Provider:
    provider_name = "deepseek_native"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def search(self, query, freshness, max_results):
        self.calls.append((query, freshness, max_results))
        if self.error is not None:
            raise self.error
        return self.result


def test_smoke_uses_fixed_public_query_and_prints_safe_evidence(capsys):
    provider = Provider(
        SearchProviderResult(
            summary="result",
            sources=(
                SearchSource(
                    title="DeepSeek release notes",
                    url="https://api-docs.deepseek.com/news/news250821",
                ),
            ),
            provider="deepseek_native",
            request_id="request-1",
        )
    )

    exit_code = asyncio.run(smoke.run(provider))

    output = capsys.readouterr().out
    assert exit_code == 0
    assert provider.calls == [(smoke.PUBLIC_QUERY, "month", 5)]
    assert "PASS provider=deepseek_native sources=1" in output
    assert "request_id=request-1" in output
    assert "domain=api-docs.deepseek.com" in output
    assert "summary=result" not in output


def test_smoke_reports_stable_provider_failure(capsys):
    provider = Provider(error=SearchUnavailable("provider_auth_failed"))

    exit_code = asyncio.run(smoke.run(provider))

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL provider=deepseek_native" in output
    assert "reason_code=provider_auth_failed" in output
