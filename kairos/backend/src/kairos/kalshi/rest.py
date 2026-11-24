"""Kalshi REST client (spec p.4, p.15, p.133).

* Separate read and write token buckets.
* Reads: exponential backoff with full jitter on 429/5xx/timeouts within a retry budget.
* Writes: **never retried blindly**. A timeout after submission yields ``WriteOutcome.UNKNOWN`` and the
  caller must reconcile (GET orders by client_order_id) before any retry.
* Circuit breaker opens after repeated failures.
"""
from __future__ import annotations

import asyncio
import enum
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .auth import KalshiSigner


class KalshiHTTPError(RuntimeError):
    def __init__(self, status: int, body: Any, path: str):
        super().__init__(f"Kalshi HTTP {status} on {path}")
        self.status = status
        self.body = body
        self.path = path


class CircuitOpen(RuntimeError):
    pass


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: float | None = None, clock=time.monotonic):
        self.rate = float(rate_per_s)
        self.capacity = float(burst if burst is not None else max(1.0, rate_per_s))
        self.tokens = self.capacity
        self.clock = clock
        self.t = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
        self.t = now

    def try_take(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    async def take(self, n: float = 1.0) -> None:
        async with self._lock:
            while not self.try_take(n):
                await asyncio.sleep(max(0.005, (n - self.tokens) / self.rate))


class CircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0, clock=time.monotonic):
        self.threshold = threshold
        self.cooldown = cooldown_s
        self.failures = 0
        self.opened_at: float | None = None
        self.clock = clock

    def check(self) -> None:
        if self.opened_at is not None:
            if self.clock() - self.opened_at < self.cooldown:
                raise CircuitOpen("Kalshi circuit open")
            self.opened_at = None
            self.failures = 0

    def ok(self) -> None:
        self.failures = 0

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()


class WriteOutcome(enum.Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"  # 409 order_already_exists → reconcile
    UNKNOWN = "unknown"  # timeout/transport error after send → reconcile, never blind retry


@dataclass
class WriteResult:
    outcome: WriteOutcome
    status: int | None
    body: Any
    latency_ms: float


def backoff_delay(attempt: int, base: float = 0.25, cap: float = 8.0, rng: random.Random | None = None) -> float:
    r = rng or random
    return r.uniform(0, min(cap, base * (2 ** attempt)))


class KalshiREST:
    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None = None,
        read_rate: float = 10.0,
        write_rate: float = 5.0,
        timeout_s: float = 5.0,
        client: httpx.AsyncClient | None = None,
        max_read_retries: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.read_bucket = TokenBucket(read_rate)
        self.write_bucket = TokenBucket(write_rate)
        self.breaker = CircuitBreaker()
        self.client = client or httpx.AsyncClient(timeout=timeout_s, verify=True, http2=False)
        self.max_read_retries = max_read_retries
        self.prefix = httpx.URL(self.base_url).path.rstrip("/")  # e.g. /trade-api/v2
        self.latency_ms: list[float] = []

    async def close(self) -> None:
        await self.client.aclose()

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if self.signer is None:
            raise PermissionError("authenticated endpoint requires a Kalshi key")
        return self.signer.headers(method, self.prefix + path)

    async def get(self, path: str, params: dict[str, Any] | None = None, auth: bool = False) -> Any:
        attempt = 0
        while True:
            self.breaker.check()
            await self.read_bucket.take()
            headers = self._auth_headers("GET", path) if auth else {}
            t0 = time.perf_counter()
            try:
                r = await self.client.get(self.base_url + path, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError):
                self.breaker.fail()
                if attempt >= self.max_read_retries:
                    raise
                await asyncio.sleep(backoff_delay(attempt))
                attempt += 1
                continue
            self.latency_ms.append((time.perf_counter() - t0) * 1000)
            if r.status_code == 429 or r.status_code >= 500:
                self.breaker.fail()
                if attempt >= self.max_read_retries:
                    raise KalshiHTTPError(r.status_code, _body(r), path)
                await asyncio.sleep(backoff_delay(attempt))
                attempt += 1
                continue
            if r.status_code >= 400:
                raise KalshiHTTPError(r.status_code, _body(r), path)
            self.breaker.ok()
            return r.json()

    async def write(self, method: str, path: str, body: dict[str, Any] | None = None) -> WriteResult:
        """Single attempt. Caller handles reconciliation for UNKNOWN/DUPLICATE."""
        self.breaker.check()
        await self.write_bucket.take()
        headers = self._auth_headers(method, path)
        headers["Content-Type"] = "application/json"
        t0 = time.perf_counter()
        try:
            r = await self.client.request(method, self.base_url + path, json=body, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            self.breaker.fail()
            return WriteResult(WriteOutcome.UNKNOWN, None, repr(exc), (time.perf_counter() - t0) * 1000)
        lat = (time.perf_counter() - t0) * 1000
        b = _body(r)
        if r.status_code in (200, 201):
            self.breaker.ok()
            return WriteResult(WriteOutcome.ACCEPTED, r.status_code, b, lat)
        if r.status_code == 409:
            return WriteResult(WriteOutcome.DUPLICATE, r.status_code, b, lat)
        if r.status_code == 429 or r.status_code >= 500:
            self.breaker.fail()
            # A 5xx after send is ambiguous: treat as unknown → reconcile.
            return WriteResult(WriteOutcome.UNKNOWN if r.status_code >= 500 else WriteOutcome.REJECTED, r.status_code, b, lat)
        return WriteResult(WriteOutcome.REJECTED, r.status_code, b, lat)

    # ---- public market data -------------------------------------------------------------
    async def exchange_status(self) -> Any:
        return await self.get("/exchange/status")

    async def series(self, series_ticker: str) -> Any:
        return await self.get(f"/series/{series_ticker}")

    async def markets(self, **params: Any) -> Any:
        return await self.get("/markets", params={k: v for k, v in params.items() if v is not None})

    async def market(self, ticker: str) -> Any:
        return await self.get(f"/markets/{ticker}")

    async def orderbook(self, ticker: str, depth: int | None = None) -> Any:
        return await self.get(f"/markets/{ticker}/orderbook", params={"depth": depth} if depth else None)

    async def trades(self, ticker: str, limit: int = 100, min_ts: int | None = None, cursor: str | None = None) -> Any:
        params = {"ticker": ticker, "limit": limit, "min_ts": min_ts, "cursor": cursor}
        return await self.get("/markets/trades", params={k: v for k, v in params.items() if v is not None})

    async def candlesticks(self, series: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1) -> Any:
        return await self.get(
            f"/series/{series}/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )

    # ---- authenticated portfolio ----------------------------------------------------------
    async def balance(self) -> Any:
        return await self.get("/portfolio/balance", auth=True)

    async def positions(self, **params: Any) -> Any:
        return await self.get("/portfolio/positions", params=params or None, auth=True)

    async def orders(self, **params: Any) -> Any:
        return await self.get("/portfolio/orders", params=params or None, auth=True)

    async def fills(self, **params: Any) -> Any:
        return await self.get("/portfolio/fills", params=params or None, auth=True)

    async def settlements(self, **params: Any) -> Any:
        return await self.get("/portfolio/settlements", params=params or None, auth=True)


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return r.text[:500]
