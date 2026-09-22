from pathlib import Path
import asyncio
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_runtime_app.server import ResearchRuntime
from research_runtime_app.browser import PlaywrightBrowserProvider


def test_runtime_rejects_private_open_url_before_network_request(tmp_path):
    runtime = ResearchRuntime(workspace=tmp_path)
    with pytest.raises(ValueError, match="private"):
        runtime.open_url({"topic": "test", "url": "https://127.0.0.1/"})


def test_runtime_request_rejects_private_payload_fields(tmp_path):
    runtime = ResearchRuntime(workspace=tmp_path)
    with pytest.raises(ValueError, match="private"):
        runtime.search({"topic": "news", "calendar": "today"})


def test_search_returns_candidates_and_routes_github_source(tmp_path):
    runtime = ResearchRuntime(workspace=tmp_path)
    calls = []
    runtime.github.search_repositories = lambda query, **kwargs: calls.append(query) or {
        "items": [{"full_name": "openai/example", "html_url": "https://github.com/openai/example", "description": "demo"}]
    }
    result = runtime.search({"topic": "agent", "source_kinds": ["github"], "max_searches": 1})
    assert result["candidate_only"] is True
    assert result["results"][0]["source_kind"] == "github"
    assert calls == ["agent"]


def test_search_routes_api_source_to_fixed_reviewed_adapter(tmp_path):
    runtime = ResearchRuntime(workspace=tmp_path)
    calls = []
    runtime.public_api.search = lambda query, **kwargs: calls.append(query) or [{
        "candidate_id": "api-1", "source_kind": "api", "title": "Story",
        "url": "https://hn.algolia.com/api/v1/items/1",
    }]
    result = runtime.search({"topic": "agent", "source_kinds": ["api"], "max_searches": 1})
    assert result["results"][0]["source_kind"] == "api"
    assert calls == ["agent"]


def test_job_budget_is_cumulative_across_runtime_calls(tmp_path):
    runtime = ResearchRuntime(workspace=tmp_path)
    runtime.search_client.search = lambda *_args, **_kwargs: []
    payload = {"topic": "agent", "job_id": "job-1", "max_searches": 1}
    assert runtime.search(payload)["job_id"] == "job-1"
    with pytest.raises(ValueError, match="research_budget_exhausted"):
        runtime.search(payload)


def test_exec_is_disabled_by_default_and_runtime_can_require_token(tmp_path, monkeypatch):
    runtime = ResearchRuntime(workspace=tmp_path)
    with pytest.raises(ValueError, match="research_exec_disabled"):
        asyncio.run(runtime.exec_public({"topic": "local", "argv": ["python3", "-c", "print(1)"]}))
    monkeypatch.setenv("RESEARCH_RUNTIME_REQUIRE_TOKEN", "true")
    with pytest.raises(RuntimeError, match="TOKEN"):
        ResearchRuntime(workspace=tmp_path)


def test_browser_without_optional_dependency_returns_stable_failure(tmp_path, monkeypatch):
    provider = PlaywrightBrowserProvider()
    monkeypatch.setattr("research_runtime_app.browser.validate_public_url", lambda url: (url, ("8.8.8.8",)))

    async def missing_browser():
        raise RuntimeError("playwright_unavailable")

    monkeypatch.setattr(provider, "_ensure_browser", missing_browser)
    result = asyncio.run(provider.open("https://example.com/"))
    assert result.ok is False
    assert result.reason_code == "playwright_unavailable"
