"""Primary/fallback wire-pod HTTP session failover. 

No Home Assistant imports, so transport failover rules are testable standalone.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import aiohttp

FAILOVER_STATUSES = (502, 504)
MIN_TIMEOUT_S = 0.05


def normalize_pod_url(url: object, *, allow_empty: bool = False) -> str:
    """Validate and strip trailing slash from a wire-pod URL."""
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        if allow_empty:
            return ""
        raise ValueError("URL must start with http:// or https://")
    if not cleaned.startswith(("http://", "https://")):
        raise ValueError("URL must start with http:// or https://")
    return cleaned


def _cap_connect_timeout(kwargs: dict[str, Any], connect_s: float) -> dict[str, Any]:
    """Return kwargs with sock_connect capped on the first candidate so failover is fast."""
    assert connect_s > 0
    out = dict(kwargs)
    tmo = out.get("timeout")
    if not isinstance(tmo, aiohttp.ClientTimeout):
        return out
    cur = tmo.sock_connect
    cap = connect_s if tmo.total is None else min(connect_s, max(MIN_TIMEOUT_S, float(tmo.total)))
    if cur is None or cur > cap:
        out["timeout"] = aiohttp.ClientTimeout(
            total=tmo.total, connect=tmo.connect, sock_connect=cap, sock_read=tmo.sock_read)
    return out


class _FailoverRequest:
    """Supports both `async with session.get(...)` and `resp = await session.get(...)`."""

    def __init__(self, runner: Callable[[], Any]) -> None:
        assert callable(runner)
        self._runner = runner
        self._resp: Any = None

    async def _open(self) -> Any:
        self._resp = await self._runner()
        return self._resp

    def __await__(self):
        return self._open().__await__()

    async def __aenter__(self) -> Any:
        return await self._open()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._resp is not None:
            close = getattr(self._resp, "release", None) or getattr(self._resp, "close", None)
            if callable(close):
                close()
            self._resp = None
        return False


class FailoverSession:
    """aiohttp session wrapper that fails over from primary_url to fallback_url on outages."""

    def __init__(self, raw_session: Any, primary_url: str, fallback_url: str = "", *,
                 clock: Callable[[], float] = time.monotonic, primary_retry_s: float = 120.0,
                 connect_timeout_s: float = 5.0) -> None:
        assert raw_session is not None and callable(clock)
        assert primary_retry_s >= 0 and connect_timeout_s > 0
        self._raw = raw_session
        self.primary_url = normalize_pod_url(primary_url)
        fb = normalize_pod_url(fallback_url, allow_empty=True)
        self.fallback_url = "" if fb == self.primary_url else fb
        self._clock = clock
        self._primary_retry_s = float(primary_retry_s)
        self._connect_timeout_s = float(connect_timeout_s)
        self._active_url = self.primary_url
        self._fallback_since: float | None = None
        self.failovers = 0

    @property
    def active_url(self) -> str:
        """Base URL currently preferred for requests."""
        return self._active_url

    def _candidates(self, url: str) -> list[tuple[str, str]]:
        """Ordered (base_url, full_url) candidates to attempt for url."""
        assert isinstance(url, str) and bool(url)
        p = self.primary_url
        if not self.fallback_url or not (url == p or url.startswith((p + "/", p + "?"))):
            return [(self._active_url, url)]
        suffix = url[len(p):]
        on_fallback = self._active_url == self.fallback_url and self._fallback_since is not None
        if on_fallback and (self._clock() - self._fallback_since) < self._primary_retry_s:
            bases = (self.fallback_url, p)
        else:
            bases = (p, self.fallback_url)
        return [(b, b + suffix) for b in bases]

    def _record_success(self, base: str) -> None:
        if base != self._active_url:
            self.failovers += 1
            self._active_url = base
        if base == self.fallback_url:
            if self._fallback_since is None:
                self._fallback_since = self._clock()
        else:
            self._fallback_since = None

    async def _send(self, method: str, url: str, **kwargs: Any) -> Any:
        candidates = self._candidates(url)
        last_exc: Exception | None = None
        for idx, (base, target) in enumerate(candidates):
            has_next = idx + 1 < len(candidates)
            kw = _cap_connect_timeout(kwargs, self._connect_timeout_s) if has_next else kwargs
            try:
                resp = await self._raw.request(method, target, **kw)
            except (aiohttp.ClientError, TimeoutError) as exc:
                last_exc = exc
                if has_next:
                    continue
                raise
            if has_next and getattr(resp, "status", 200) in FAILOVER_STATUSES:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
                last_exc = aiohttp.ClientResponseError(
                    getattr(resp, "request_info", None), (), status=resp.status)
                continue
            self._record_success(base)
            return resp
        assert last_exc is not None
        raise last_exc

    def request(self, method: str, url: str, **kwargs: Any) -> _FailoverRequest:
        return _FailoverRequest(lambda: self._send(method, url, **kwargs))

    def get(self, url: str, **kwargs: Any) -> _FailoverRequest:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _FailoverRequest:
        return self.request("POST", url, **kwargs)
