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
        "aid": 123456,
        "cid": 111111,
        "title": "公开测试视频",
        "desc": "视频简介",
        "duration": 523,
        "pubdate": 1_700_000_000,
        "pic": "//i0.hdslb.com/test.jpg",
        "owner": {"name": "公开作者"},
    }
    data.update(overrides)
    return json.dumps({"code": 0, "data": data}, ensure_ascii=False)


def player_payload(subtitles=None, *, need_login_subtitle=False):
    return json.dumps(
        {
            "code": 0,
            "data": {
                "need_login_subtitle": need_login_subtitle,
                "subtitle": {"subtitles": list(subtitles or [])},
            },
        },
        ensure_ascii=False,
    )


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


def test_view_metadata_supplies_cid_not_subtitle():
    fetcher = FakeFetcher(
        [
            response(metadata_payload()),
            response(player_payload([])),
        ]
    )
    adapter = make_adapter(fetcher)
    metadata = asyncio.run(
        adapter.resolve_metadata("https://www.bilibili.com/video/BV1xx411c7mD")
    )

    asyncio.run(adapter.resolve_transcript(metadata))

    assert fetcher.calls[1]["url"] == (
        "https://api.bilibili.com/x/player/wbi/v2?bvid=BV1xx411c7mD&cid=111111"
    )


def test_player_v2_subtitles_are_discovered_from_subtitles_field():
    subtitle_url = "https://aisubtitle.example.test/subtitle.json"
    subtitle = json.dumps(
        {
            "body": [
                {"from": 20.5, "to": 22.0, "content": "第二句"},
                {"from": 0, "to": 2.5, "content": "第一句"},
                {"from": 2.5, "to": 4.0, "content": "   "},
                {"content": "没有时间戳"},
            ]
        },
        ensure_ascii=False,
    )
    fetcher = FakeFetcher(
        [
            response(
                metadata_payload()
            ),
            response(player_payload([{"lan": "ai-zh", "subtitle_url": subtitle_url}])),
            response(subtitle, content_type="application/json"),
        ]
    )
    adapter = make_adapter(fetcher)
    metadata = asyncio.run(
        adapter.resolve_metadata("https://www.bilibili.com/video/BV1xx411c7mD")
    )

    transcript = asyncio.run(adapter.resolve_transcript(metadata))

    assert transcript.language == "zh-CN"
    assert [segment.text for segment in transcript.segments] == [
        "第一句",
        "第二句",
        "没有时间戳",
    ]
    assert transcript.segments[0].start_seconds == 0.0
    assert transcript.segments[1].end_seconds == 22.0
    assert transcript.segments[2].start_seconds is None
    assert transcript.total_chars == len("第一句\n第二句\n没有时间戳")


def test_need_login_subtitle_without_tracks_returns_no_public_subtitle():
    fetcher = FakeFetcher(
        [response(metadata_payload()), response(player_payload([], need_login_subtitle=True))]
    )
    adapter = make_adapter(fetcher)
    metadata = asyncio.run(
        adapter.resolve_metadata("https://www.bilibili.com/video/BV1xx411c7mD")
    )

    transcript = asyncio.run(adapter.resolve_transcript(metadata))

    assert transcript.segments == ()
    assert transcript.extraction_mode == "no_public_subtitle"
    assert transcript.total_chars == 0


def test_public_subtitle_track_is_downloaded():
    subtitle_url = "https://aisubtitle.example.test/subtitle.json"
    fetcher = FakeFetcher(
        [
            response(metadata_payload()),
            response(player_payload([{"lan": "en", "subtitle_url": subtitle_url}])),
            response(json.dumps({"body": [{"from": 0, "to": 1, "content": "hello"}]})),
        ]
    )
    adapter = make_adapter(fetcher)
    metadata = asyncio.run(
        adapter.resolve_metadata("https://www.bilibili.com/video/BV1xx411c7mD")
    )

    transcript = asyncio.run(adapter.resolve_transcript(metadata))

    assert transcript.segments[0].text == "hello"
    assert [call["url"] for call in fetcher.calls] == [
        "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx411c7mD",
        "https://api.bilibili.com/x/player/wbi/v2?bvid=BV1xx411c7mD&cid=111111",
        subtitle_url,
    ]


def test_part_2_uses_page_2_cid_and_preserves_canonical_url():
    pages = [
        {"page": 1, "cid": 111111, "part": "第一 P", "duration": 10},
        {"page": 2, "cid": 222222, "part": "第二 P", "duration": 20},
    ]
    fetcher = FakeFetcher(
        [response(metadata_payload(pages=pages)), response(player_payload([]))]
    )
    adapter = make_adapter(fetcher)
    metadata = asyncio.run(
        adapter.resolve_metadata(
            "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
        )
    )

    asyncio.run(adapter.resolve_transcript(metadata))

    assert metadata.canonical_url == "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
    assert metadata.title == "第二 P"
    assert metadata.duration_seconds == 20
    assert fetcher.calls[1]["url"].endswith("bvid=BV1xx411c7mD&cid=222222")


def test_part_2_does_not_fall_back_to_part_1():
    fetcher = FakeFetcher(
        [
            response(
                metadata_payload(
                    pages=[{"page": 1, "cid": 111111, "part": "第一 P"}]
                )
            )
        ]
    )
    adapter = make_adapter(fetcher)

    with pytest.raises(VideoProviderError) as exc_info:
        asyncio.run(
            adapter.resolve_metadata(
                "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
            )
        )

    assert exc_info.value.reason_code == "unsupported_video_variant"
    assert len(fetcher.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bilibili.com/video/BV1xx411c7mD",
        "https://user:pass@www.bilibili.com/video/BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1xx411c7mD?access_token=secret",
        "https://127.0.0.1/video/BV1xx411c7mD",
    ],
)
def test_bilibili_user_url_reuses_public_url_safety_gate(url):
    adapter = make_adapter(FakeFetcher([]))

    with pytest.raises(VideoProviderError) as exc_info:
        asyncio.run(adapter.resolve_metadata(url))

    assert exc_info.value.reason_code == "video_url_not_safe"


def test_bilibili_redirect_to_private_target_is_rejected_before_api_call():
    fetcher = FakeFetcher(
        [
            response(
                "",
                status=302,
                content_type="text/html",
                location="https://127.0.0.1/video/BV1xx411c7mD",
            )
        ]
    )
    adapter = make_adapter(fetcher)

    with pytest.raises(VideoProviderError) as exc_info:
        asyncio.run(adapter.resolve_metadata("https://b23.tv/private"))

    assert exc_info.value.reason_code == "video_url_not_safe"
    assert len(fetcher.calls) == 1


def test_bilibili_invalid_timestamp_is_not_invented():
    normalized = BilibiliVideoAdapter._normalize_segments(
        [{"from": 10, "to": 2, "content": "out of order"}]
    )

    assert normalized[0].start_seconds is None
    assert normalized[0].end_seconds is None
