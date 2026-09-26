"""Minimal client for MTD's REST API v3 (https://api.mtd.dev, header X-ApiKey).

The key has an hourly rate limit per developer, so results are cached and a
429 puts the client in back-off until the server's retry-after passes.
"""
import logging
import time

import httpx

from . import config

log = logging.getLogger(__name__)


class MtdRestError(Exception):
    pass


class MtdRestClient:
    def __init__(self, api_key: str = config.MTD_API_KEY, base_url: str = config.MTD_API_BASE,
                 cache_ttl_s: int = 24 * 3600, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.cache_ttl_s = cache_ttl_s
        self._client = httpx.Client(base_url=base_url, timeout=5, transport=transport,
                                    headers={"X-ApiKey": api_key})
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._blocked_until = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and time.time() >= self._blocked_until

    def _get(self, path: str, params: dict) -> object:
        resp = self._client.get(path, params=params)
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After")
            self._blocked_until = time.time() + (int(retry) if retry and retry.isdigit() else 600)
            raise MtdRestError("rate limited")
        if resp.status_code >= 400:
            raise MtdRestError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        err = body.get("error") or body.get("Error")
        if err:
            raise MtdRestError(str(err))
        return body.get("result", body.get("Result"))

    def keep_warm(self, interval_s: int = 600, first_timeout_s: float = 60) -> None:
        """Ping the API forever (run in a daemon thread).

        MTD's API cold-starts after sitting idle (~30 s for the first request), which
        would make the first search fall back to local results. One cheap call every
        10 minutes keeps it responsive at ~6 requests/hour.
        """
        timeout = first_timeout_s
        while True:
            if self.enabled:
                t0 = time.time()
                try:
                    self._client.get("/stops/IT", timeout=timeout)
                    log.info("MTD REST API warm (%.1fs)", time.time() - t0)
                except Exception as e:
                    log.warning("MTD REST API warm-up failed: %s", e)
            timeout = 30
            time.sleep(interval_s)

    def search_stops(self, query: str) -> list[dict]:
        """Raw StopSearchResult dicts (stopId, name, subName, city, ...)."""
        if not self.enabled:
            raise MtdRestError("REST API disabled (no key or rate limited)")
        key = query.strip().lower()
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.cache_ttl_s:
            return hit[1]
        result = self._get("/stops/search", {"query": query[:50]}) or []
        self._cache[key] = (time.time(), result)
        return result
