"""Browser boundary.  It intentionally carries no cookies or login state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .policy import validate_public_url


@dataclass(frozen=True)
class BrowserResult:
    ok: bool
    url: str
    title: str = ""
    text: str = ""
    links: tuple[str, ...] = ()
    screenshot: str | None = None
    reason_code: str | None = None


class BrowserProvider(Protocol):
    async def open(self, url: str, *, wait_seconds: float = 2.0) -> BrowserResult: ...


class UnavailableBrowser:
    async def open(self, url: str, *, wait_seconds: float = 2.0) -> BrowserResult:
        canonical, _ = validate_public_url(url)
        return BrowserResult(False, canonical, reason_code="browser_unavailable")

