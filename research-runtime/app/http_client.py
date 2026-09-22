"""Public-only HTTP transport with redirect and response bounds."""

from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import urljoin, urlsplit
import http.client
import socket
import ssl

import httpx

from .extraction import ExtractedDocument, extract
from .policy import validate_public_url


@dataclass(frozen=True)
class PublicResponse:
    url: str
    status_code: int
    content_type: str
    body: bytes
    redirect_count: int
    headers: dict[str, str] | None = None


class PublicHttpClient:
    def __init__(self, *, timeout_seconds: float = 10, max_bytes: int = 2 * 1024 * 1024, max_redirects: int = 3) -> None:
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.max_bytes = max(1024, int(max_bytes))
        self.max_redirects = max(0, min(int(max_redirects), 10))

    def fetch(self, url: str, *, extra_headers: dict[str, str] | None = None) -> PublicResponse:
        current, _ = validate_public_url(url)
        headers = {
            "User-Agent": "MindFlowResearch/1.0 (+public-research)",
            "Accept": "text/html, text/plain, application/json, text/markdown;q=0.9, */*;q=0.1",
            "Accept-Encoding": "identity",
        }
        headers.update(extra_headers or {})
        for redirect_count in range(self.max_redirects + 1):
            current, addresses = validate_public_url(current)
            response = self._fetch_pinned(current, addresses, headers)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    break
                current = urljoin(current, location)
                continue
            if response.status_code in {401, 403}:
                raise ValueError("authentication_required")
            if response.status_code >= 400:
                raise ValueError("fetch_failed")
            if len(response.body) > self.max_bytes:
                raise ValueError("response_too_large")
            return PublicResponse(
                url=current,
                status_code=response.status_code,
                content_type=response.headers.get("content-type", "text/plain").split(";", 1)[0],
                body=response.body,
                redirect_count=redirect_count,
                headers=response.headers,
            )
        raise ValueError("too_many_redirects")

    def _fetch_pinned(self, url: str, addresses: tuple[str, ...], headers: dict[str, str]) -> "_PinnedResponse":
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        port = parsed.port or 443
        last_error: Exception | None = None
        for address in addresses:
            sock: socket.socket | ssl.SSLSocket | None = None
            try:
                sock = socket.create_connection((address, port), timeout=self.timeout_seconds)
                context = ssl.create_default_context()
                tls = context.wrap_socket(sock, server_hostname=parsed.hostname)
                sock = tls
                request_headers = {
                    **headers,
                    "Host": parsed.hostname if port == 443 else f"{parsed.hostname}:{port}",
                    "Connection": "close",
                }
                request = "GET " + path + " HTTP/1.1\r\n" + "".join(
                    f"{key}: {value}\r\n" for key, value in request_headers.items()
                ) + "\r\n"
                tls.sendall(request.encode("ascii", "strict"))
                response = http.client.HTTPResponse(tls, method="GET")
                response.begin()
                body = response.read(self.max_bytes + 1)
                response_headers = {key.casefold(): value for key, value in response.getheaders()}
                return _PinnedResponse(response.status, response_headers, body)
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
            finally:
                if sock is not None:
                    sock.close()
        raise ValueError("public connection failed") from last_error

    def read_document(self, url: str) -> tuple[PublicResponse, ExtractedDocument]:
        response = self.fetch(url)
        document = extract(response.body, content_type=response.content_type)
        return response, document


@dataclass(frozen=True)
class _PinnedResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes

    def read_document(self, url: str) -> tuple[PublicResponse, ExtractedDocument]:
        response = self.fetch(url)
        document = extract(response.body, content_type=response.content_type)
        return response, document
