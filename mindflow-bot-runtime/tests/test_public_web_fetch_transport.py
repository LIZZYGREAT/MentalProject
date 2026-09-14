import asyncio
import socket

from app.repositories_web_document import WebDocumentRepository
from app.services.public_web_document_service import (
    PinnedHttpsWebFetcher,
    PublicWebDocumentService,
    RawWebResponse,
)
from helpers import memory_database, participant


class _Reader:
    def __init__(self, *, body=b"ok"):
        self.lines = [
            b"HTTP/1.1 200 OK\r\n",
            f"Content-Length: {len(body)}\r\n".encode(),
            b"Content-Type: text/plain\r\n",
            b"\r\n",
        ]
        self.body = body

    async def readline(self):
        return self.lines.pop(0) if self.lines else b""

    async def readexactly(self, size):
        assert size == len(self.body)
        return self.body

    async def read(self, _size):
        return b""


class _Writer:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, value):
        self.writes.append(value)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def test_public_fetch_uses_only_prevalidated_ip(monkeypatch):
    seen = {}

    async def open_connection(**kwargs):
        seen.update(kwargs)
        return _Reader(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    result = asyncio.run(PinnedHttpsWebFetcher().fetch(
        "https://public.example/article",
        resolved_addresses=("8.8.8.8",),
        server_hostname="public.example",
        timeout_seconds=1,
        max_bytes=1024,
    ))

    assert result.body == b"ok"
    assert seen["host"] == "8.8.8.8"
    assert seen["server_hostname"] == "public.example"
    assert seen["port"] == 443


def test_fetcher_does_not_perform_second_dns_resolution(monkeypatch):
    async def open_connection(**_kwargs):
        return _Reader(), _Writer()

    def unexpected_dns(*_args, **_kwargs):
        raise AssertionError("fetcher must not resolve the hostname")

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected_dns)

    result = asyncio.run(PinnedHttpsWebFetcher().fetch(
        "https://public.example/",
        resolved_addresses=("1.1.1.1",),
        server_hostname="public.example",
        timeout_seconds=1,
        max_bytes=1024,
    ))

    assert result.status_code == 200


def test_dns_rebinding_cannot_switch_to_private_ip(monkeypatch):
    database = memory_database()
    user = participant(database, "URL-REBIND")
    calls = {"resolver": 0, "destination": None}

    async def resolver(_host, _port):
        calls["resolver"] += 1
        return ("8.8.8.8",) if calls["resolver"] == 1 else ("10.0.0.1",)

    async def open_connection(**kwargs):
        calls["destination"] = kwargs["host"]
        return _Reader(body=b"safe body"), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    reader = PublicWebDocumentService(
        WebDocumentRepository(database),
        resolver=resolver,
        extractor=lambda body: body,
    )

    result = asyncio.run(reader.read_url(user.id, url="https://rebind.example/a"))

    assert result["ok"] is True
    assert calls == {"resolver": 1, "destination": "8.8.8.8"}


def test_redirect_uses_new_validated_address_set():
    database = memory_database()
    user = participant(database, "URL-REDIRECT-PIN")

    class Fetcher:
        def __init__(self):
            self.calls = []

        async def fetch(self, url, **kwargs):
            self.calls.append((url, kwargs["resolved_addresses"], kwargs["server_hostname"]))
            if len(self.calls) == 1:
                return RawWebResponse(302, {"location": "https://next.example/b"}, b"")
            return RawWebResponse(200, {"content-type": "text/plain"}, b"redirected")

    async def resolver(host, _port):
        return {"first.example": ("8.8.8.8",), "next.example": ("1.1.1.1",)}[host]

    fetcher = Fetcher()
    reader = PublicWebDocumentService(
        WebDocumentRepository(database),
        fetcher=fetcher,
        resolver=resolver,
        extractor=lambda body: body,
    )

    result = asyncio.run(reader.read_url(user.id, url="https://first.example/a"))

    assert result["ok"] is True
    assert fetcher.calls == [
        ("https://first.example/a", ("8.8.8.8",), "first.example"),
        ("https://next.example/b", ("1.1.1.1",), "next.example"),
    ]


def test_tls_sni_remains_original_hostname_when_ip_is_pinned(monkeypatch):
    seen = {}

    async def open_connection(**kwargs):
        seen.update(kwargs)
        return _Reader(), _Writer()

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    asyncio.run(PinnedHttpsWebFetcher().fetch(
        "https://news.example/article",
        resolved_addresses=("2606:4700:4700::1111",),
        server_hostname="news.example",
        timeout_seconds=1,
        max_bytes=1024,
    ))

    assert seen["host"] == "2606:4700:4700::1111"
    assert seen["server_hostname"] == "news.example"
    assert seen["ssl"].check_hostname is True
