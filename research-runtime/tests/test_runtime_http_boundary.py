from pathlib import Path
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
