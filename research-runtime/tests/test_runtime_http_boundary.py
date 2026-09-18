from pathlib import Path
import asyncio
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_runtime_app.server import ResearchRuntime


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
