"""Async client for the Duquesne collector's read-only JSON API (collector/README.md)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT

SERVICE_NAME = "duquesne-collector"


class CollectorError(Exception):
    """The collector couldn't be reached or answered with an error."""


class CollectorAuthError(CollectorError):
    """The collector wants a (different) bearer token."""


class NotACollectorError(CollectorError):
    """Something answered, but not a Duquesne collector."""


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = f"http://{url}"
    return url


class CollectorClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str | None = None) -> None:
        self._session = session
        self.url = normalize_url(url)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with self._session.get(
                f"{self.url}{path}",
                params=params,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status == 401:
                    raise CollectorAuthError("collector rejected the API token")
                if resp.status >= 400:
                    raise CollectorError(f"{path} returned HTTP {resp.status}")
                try:
                    return await resp.json(content_type=None)
                except ValueError as err:
                    raise NotACollectorError(f"{path} did not return JSON") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CollectorError(f"can't reach the collector at {self.url}: {err}") from err

    async def status(self) -> dict[str, Any]:
        body = await self._get("/status")
        if not isinstance(body, dict) or body.get("service") != SERVICE_NAME:
            raise NotACollectorError("not a Duquesne collector (/status has no service name)")
        return body

    async def bills(self) -> dict[str, Any]:
        return await self._get("/bills")

    async def usage(self, start: datetime | None) -> dict[str, Any]:
        """Hourly and daily usage from `start` (UTC, inclusive) to now; everything if None."""
        if start is None:
            begin = "2000-01-01"
        else:
            begin = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return await self._get("/usage", {"start": begin})
