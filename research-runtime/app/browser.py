"""Browser boundary with a fresh, public-only Playwright context per request."""

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


class PlaywrightBrowserProvider:
    """Read-only browser provider; agent input never becomes JavaScript."""

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = bool(headless)
        self._playwright = None
        self._browser = None

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("playwright_unavailable") from exc
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        return self._browser

    async def open(self, url: str, *, wait_seconds: float = 2.0) -> BrowserResult:
        canonical, _ = validate_public_url(url)
        browser = await self._ensure_browser()
        context = await browser.new_context(
            accept_downloads=False,
            java_script_enabled=True,
            service_workers="block",
        )

        async def public_route(route):
            request_url = route.request.url
            if request_url.startswith("about:blank"):
                await route.continue_()
                return
            try:
                validate_public_url(request_url)
            except ValueError:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await context.route("**/*", public_route)
        page = await context.new_page()
        try:
            await page.goto(canonical, wait_until="domcontentloaded", timeout=15_000)
            await page.wait_for_timeout(max(0, min(int(wait_seconds * 1000), 10_000)))
            title = await page.title()
            text = (await page.locator("body").inner_text(timeout=5_000))[:60_000]
            links = await page.locator("a[href]").evaluate_all(
                "els => els.map(el => el.href).filter(Boolean).slice(0, 100)"
            )
            safe_links: list[str] = []
            for link in links:
                try:
                    safe_links.append(validate_public_url(str(link))[0])
                except ValueError:
                    continue
            return BrowserResult(True, page.url, title[:300], text, tuple(dict.fromkeys(safe_links)))
        except Exception as exc:
            return BrowserResult(False, page.url or canonical, reason_code=type(exc).__name__.lower())
        finally:
            await context.close()

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
