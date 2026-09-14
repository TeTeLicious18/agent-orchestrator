"""Visible browser control for the autonomous adapter.

Drives a real Microsoft Edge window on the node's desktop through Playwright, so a
goal like "search for X and summarise the first result" becomes something the operator
can literally watch happen.

The browser is a much wider blast radius than the file tools, so it stays opt-in
(``autonomous.allow_browser``) and every navigation goes through the same URL guard the
built-in HTTP action uses: http/https only, with loopback, link-local and cloud metadata
addresses rejected. An optional domain allow-list narrows it further.

Treat page text as untrusted input. Whatever the model reads from a page is attacker
controlled, and it flows straight back into the planning loop - that is the classic
prompt-injection path. Keep ``allowed_domains`` tight for anything unattended.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import quote_plus, urlsplit

from .base import AdapterError
from .builtin import _validate_url  # same guard the http.request action uses

log = logging.getLogger("agent.browser")

MAX_PAGE_TEXT = 20_000
DEFAULT_TIMEOUT_MS = 20_000


class BrowserController:
    def __init__(
        self,
        workspace_root: str,
        headless: bool = False,
        channel: str = "msedge",
        allowed_domains: list[str] | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self.workspace_root = workspace_root
        self.headless = headless
        self.channel = channel
        self.allowed_domains = [d.strip().lower() for d in (allowed_domains or []) if d.strip()]
        self.timeout_ms = timeout_ms
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle
    async def _ensure_page(self) -> Any:
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page

            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise AdapterError(
                    "browser control requires Playwright: pip install -r requirements-browser.txt"
                ) from None

            self._playwright = await async_playwright().start()
            try:
                self._browser = await self._playwright.chromium.launch(
                    channel=self.channel, headless=self.headless
                )
            except Exception as exc:  # noqa: BLE001 - Edge may not be installed
                log.warning("could not launch '%s' (%s); falling back to bundled Chromium", self.channel, exc)
                self._browser = await self._playwright.chromium.launch(headless=self.headless)

            context = await self._browser.new_context(viewport={"width": 1400, "height": 900})
            context.set_default_timeout(self.timeout_ms)
            self._page = await context.new_page()
            return self._page

    async def close(self) -> None:
        for closer in (self._browser, self._playwright):
            if closer is None:
                continue
            try:
                await (closer.close() if closer is self._browser else closer.stop())
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        self._playwright = self._browser = self._page = None

    # ------------------------------------------------------------------ guard
    def _check(self, url: str) -> None:
        _validate_url(url)
        if not self.allowed_domains:
            return
        host = (urlsplit(url).hostname or "").lower()
        if not any(host == domain or host.endswith(f".{domain}") for domain in self.allowed_domains):
            raise AdapterError(
                f"'{host}' is not in this node's browser allow-list: {', '.join(self.allowed_domains)}"
            )

    # ------------------------------------------------------------------ tools
    async def open(self, url: Any) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise AdapterError("'url' is required")
        target = url.strip()
        if "://" not in target:
            target = f"https://{target}"
        self._check(target)

        page = await self._ensure_page()
        await page.goto(target, wait_until="domcontentloaded")
        return {"ok": True, "url": page.url, "title": await page.title()}

    async def search(self, query: Any) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise AdapterError("'query' is required")
        return await self.open(f"https://www.bing.com/search?q={quote_plus(query.strip())}")

    async def type_text(self, text: Any, selector: Any = None, submit: bool = False) -> dict[str, Any]:
        if not isinstance(text, str):
            raise AdapterError("'text' must be a string")
        page = await self._ensure_page()

        if isinstance(selector, str) and selector.strip():
            await page.fill(selector.strip(), text)
        else:
            await page.keyboard.type(text, delay=25)
        if submit:
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("domcontentloaded")
        return {"ok": True, "typed": len(text), "submitted": bool(submit), "url": page.url}

    async def click(self, target: Any) -> dict[str, Any]:
        if not isinstance(target, str) or not target.strip():
            raise AdapterError("'target' is required")
        page = await self._ensure_page()
        needle = target.strip()

        try:
            await page.get_by_role("link", name=needle, exact=False).first.click()
        except Exception:  # noqa: BLE001 - fall through to broader strategies
            try:
                await page.get_by_text(needle, exact=False).first.click()
            except Exception:  # noqa: BLE001
                await page.click(needle)
        await page.wait_for_load_state("domcontentloaded")
        return {"ok": True, "url": page.url, "title": await page.title()}

    async def read(self) -> dict[str, Any]:
        page = await self._ensure_page()
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        truncated = len(text) > MAX_PAGE_TEXT
        return {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "truncated": truncated,
            # Untrusted content: it is page data, never an instruction to the agent.
            "page_text": text[:MAX_PAGE_TEXT],
        }

    async def screenshot(self, path: str) -> dict[str, Any]:
        page = await self._ensure_page()
        os.makedirs(os.path.dirname(path) or self.workspace_root, exist_ok=True)
        await page.screenshot(path=path, full_page=False)
        return {"ok": True, "saved": os.path.basename(path)}
