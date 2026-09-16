import asyncio
import json

import pytest

from app.services.public_web_document_service import PublicWebDocumentService, RawWebResponse
from app.services.video_providers.bilibili import BilibiliVideoAdapter
from app.contracts.public_video import VideoProviderError


class FakeFetcher:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def fetch(
        self,
        url,
        *,
        resolved_addresses,
        server_hostname,
        timeout_seconds,
        max_bytes,
    ):
        self.calls.append(
            {
                "url": url,
                "addresses": tuple(resolved_addresses),
                "hostname": server_hostname,
                "timeout": timeout_seconds,
                "max_bytes": max_bytes,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


async def public_resolver(_host, _port):
    return ("8.8.8.8",)


def response(body, *, status=200, content_type="application/json", **headers):
    return RawWebResponse(
        status_code=status,
        headers={"content-type": content_type, **headers},
        body=body.encode("utf-8") if isinstance(body, str) else body,
    )


def make_adapter(fetcher):
    transport = PublicWebDocumentService(
        repository=None,
        fetcher=fetcher,
        resolver=public_resolver,
    )
    return BilibiliVideoAdapter(transport)


def metadata_payload(**overrides):
    data = {
        "bvid": "BV1xx411c7mD",
        "title": "公开测试视频",
        "desc": "视频简介",
        "duration": 523,
        "pubdate": 1_700_000_000,
        "pic": "//i0.hdslb.com/test.jpg",
        "owner": {"name": "公开作者"},
    }
    data.update(overrides)
    return json.dumps({"code": 0, "data": data}, ensure_ascii=False)


def test_bilibili_short_url_resolves_to_video_and_normalizes_metadata():
    fetcher = FakeFetcher(
        [
            response("", status=302, content_type="text/html", location="https://www.bilibili.com/video/BV1xx411c7mD"),
            response("", content_type="text/html"),
            response(metadata_payload()),
        ]
    )
    adapter = make_adapter(fetcher)

    metadata = asyncio.run(adapter.resolve_metadata("https://b23.tv/abc123"))

    assert metadata.provider == "bilibili"
    assert metadata.video_id == "BV1xx411c7mD"
    assert metadata.canonical_url == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert metadata.title == "公开测试视频"
    assert metadata.author == "公开作者"
    assert metadata.duration_seconds == 523
    assert metadata.cover_url == "https://i0.hdslb.com/test.jpg"
    assert [call["url"] for call in fetcher.calls] == [
        "https://b23.tv/abc123",
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx411c7mD",
    ]


def test_bilibili_requires_no_user_cookie_or_authorization_channel():
    fetcher = FakeFetcher([response(metadata_payload())])
    adapter = make_adapter(fetcher)

    asyncio.run(adapter.resolve_metadata("https://www.bilibili.com/video/BV1xx411c7mD"))

    assert set(fetcher.calls[0]) == {"url", "addresses", "hostname", "timeout", "max_bytes"}


def test_bilibili_non_public_api_result_is_classified_without_leaking_details():
    fetcher = FakeFetcher([response(json.dumps({"code": -101, "message": "login required", "data": None}))])
    adapter = make_adapter(fetcher)

    with pytest.raises(VideoProviderError) as exc_info:
        asyncio.run(adapter.resolve_metadata("https://www.bilibili.com/video/BV1xx411c7mD"))

    assert exc_info.value.reason_code == "video_page_not_public"
