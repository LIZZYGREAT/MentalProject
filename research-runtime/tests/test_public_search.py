from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_runtime_app.search import PublicSearchClient


class _Response:
    body = b'''<a class="result__a" href="https://example.com/a">A <b>result</b></a>'''


class _Http:
    def fetch(self, _url):
        return _Response()


def test_public_search_extracts_only_https_results():
    items = PublicSearchClient(_Http()).search("public topic")
    assert items == [{"title": "A result", "url": "https://example.com/a", "snippet": ""}]

