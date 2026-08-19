"""Shared async HTTP layer for every source adapter.

Two things here are load-bearing:

1. ``BROWSER_UA`` - Ashby's public posting API returns 404 for *every* request that
   arrives without a browser User-Agent. It fails silently and totally, so the header
   is applied to all requests by default rather than left to individual adapters.
2. ETag conditional GET - the Simplify feed is ~12.5 MB. Re-downloading it every few
   minutes is wasteful when it usually has not changed.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class Fetcher:
    """Bounded-concurrency JSON fetcher with retry/backoff and ETag support."""

    def __init__(
        self,
        concurrency: int = 12,
        timeout: float = 25.0,
        retries: int = 3,
        etags: dict[str, str] | None = None,
    ) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._timeout = timeout
        self._retries = retries
        self.etags: dict[str, str] = etags if etags is not None else {}
        self.stats: dict[str, int] = {"ok": 0, "not_modified": 0, "error": 0, "missing": 0}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "Fetcher":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout),
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_json(
        self,
        url: str,
        *,
        etag_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any | None:
        """GET and decode JSON. Returns None on 304, 404, or exhausted retries."""
        return await self._request("GET", url, etag_key=etag_key, headers=headers)

    async def get_text(
        self,
        url: str,
        *,
        etag_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        """GET a plain-text body. Some feeds publish markdown rather than JSON."""
        return await self._request(
            "GET", url, etag_key=etag_key, headers=headers, as_text=True
        )

    async def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> Any | None:
        return await self._request("POST", url, json=payload, headers=headers)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        etag_key: str | None = None,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        as_text: bool = False,
    ) -> Any | None:
        if self._client is None:
            raise RuntimeError("Fetcher must be used as an async context manager")

        req_headers = dict(headers or {})
        if etag_key and (tag := self.etags.get(etag_key)):
            req_headers["If-None-Match"] = tag

        delay = 1.0
        async with self._sem:
            for attempt in range(self._retries + 1):
                try:
                    resp = await self._client.request(
                        method, url, headers=req_headers or None, json=json
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt >= self._retries:
                        log.debug("%s %s failed after retries: %s", method, url, exc)
                        self.stats["error"] += 1
                        return None
                    await self._sleep(delay)
                    delay *= 2
                    continue

                if resp.status_code == 304:
                    self.stats["not_modified"] += 1
                    return None

                if resp.status_code == 404:
                    # A dead company slug, not a transient failure - don't retry.
                    self.stats["missing"] += 1
                    return None

                if resp.status_code in RETRY_STATUS and attempt < self._retries:
                    wait = self._retry_after(resp) or delay
                    await self._sleep(wait)
                    delay *= 2
                    continue

                if resp.status_code >= 400:
                    log.debug("%s %s -> HTTP %s", method, url, resp.status_code)
                    self.stats["error"] += 1
                    return None

                if etag_key and (tag := resp.headers.get("ETag")):
                    self.etags[etag_key] = tag

                if as_text:
                    self.stats["ok"] += 1
                    return resp.text

                try:
                    data = resp.json()
                except ValueError:
                    log.debug("%s %s returned non-JSON body", method, url)
                    self.stats["error"] += 1
                    return None

                self.stats["ok"] += 1
                return data

        self.stats["error"] += 1
        return None

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return min(float(raw), 30.0)
        except ValueError:
            return None

    @staticmethod
    async def _sleep(base: float) -> None:
        # Jitter so a burst of concurrent failures doesn't retry in lockstep.
        await asyncio.sleep(base * (0.5 + random.random()))
