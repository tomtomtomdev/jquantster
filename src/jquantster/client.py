"""J-Quants V2 HTTP client with a rate limiter shared through SQLite.

The limit is per account, not per process, so every call is recorded in the
`api_calls` table and every process checks that log before calling. A 429 sets
a shared cooldown so a second process doesn't keep hammering and trigger the
API's ~5 minute account block.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from .config import BASE_URL, COOLDOWN_AFTER_429
from .db import get_meta, set_meta

WINDOW = 60.0


class JQuantsError(Exception):
    pass


class AuthError(JQuantsError):
    """The API key is missing, invalid or expired."""


class NotEntitled(JQuantsError):
    """The subscription plan doesn't include this endpoint or date range."""


class RateLimiter:
    def __init__(self, conn, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 on_wait: Callable[[float], None] | None = None):
        self.conn = conn
        self.clock = clock
        self.sleep = sleep
        self.on_wait = on_wait

    def acquire(self, buckets: list[tuple[str, int]]) -> None:
        """Block until one more call fits every (bucket, per-minute limit), then record it."""
        while True:
            wait = self._try_acquire(buckets)
            if wait <= 0:
                return
            if self.on_wait:
                self.on_wait(wait)
            self.sleep(wait)

    def _try_acquire(self, buckets) -> float:
        now = self.clock()
        self.conn.execute("BEGIN IMMEDIATE")  # serialises processes sharing the DB
        try:
            cooldown = float(get_meta(self.conn, "cooldown_until", "0"))
            if cooldown > now:
                self.conn.execute("COMMIT")
                return cooldown - now
            self.conn.execute("DELETE FROM api_calls WHERE ts < ?", (now - WINDOW,))
            wait = 0.0
            for bucket, limit in buckets:
                rows = self.conn.execute(
                    "SELECT ts FROM api_calls WHERE bucket = ? AND ts >= ? ORDER BY ts",
                    (bucket, now - WINDOW),
                ).fetchall()
                if len(rows) >= limit:
                    # The call that frees a slot is the one `limit` places back.
                    oldest_blocking = rows[len(rows) - limit][0]
                    # +2s slack for latency and clock skew against the server's window
                    wait = max(wait, oldest_blocking + WINDOW - now + 2.0)
            if wait <= 0:
                self.conn.executemany(
                    "INSERT INTO api_calls VALUES (?, ?)", [(now, b) for b, _ in buckets]
                )
            self.conn.execute("COMMIT")
            return wait
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def cooldown(self, seconds: float) -> None:
        set_meta(self.conn, "cooldown_until", str(self.clock() + seconds))


class JQuantsClient:
    def __init__(self, api_key: str, limiter: RateLimiter, budget_rpm: int,
                 fins_budget_rpm: int, transport: httpx.BaseTransport | None = None,
                 max_retries: int = 4):
        if not api_key:
            raise AuthError("JQUANTS_API_KEY is not set. Copy .env.example to .env and add your key.")
        self.limiter = limiter
        self.budget_rpm = budget_rpm
        self.fins_budget_rpm = fins_budget_rpm
        self.max_retries = max_retries
        self.calls = 0
        self.http = httpx.Client(
            base_url=BASE_URL, headers={"x-api-key": api_key},
            timeout=httpx.Timeout(60.0), transport=transport,
        )

    def _buckets(self, path: str) -> list[tuple[str, int]]:
        buckets = [("plan", self.budget_rpm)]
        if path.startswith("/fins/"):
            buckets.append(("fins", self.fins_budget_rpm))
        return buckets

    def _request(self, path: str, params: dict[str, Any]) -> dict:
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire(self._buckets(path))
            self.calls += 1
            try:
                resp = self.http.get(path, params=params)
            except httpx.TransportError:
                if attempt == self.max_retries:
                    raise
                self.limiter.sleep(5 * 3**attempt)
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 210:  # outside the plan's window or unknown code
                return {"data": []}
            message = _message(resp)
            if resp.status_code == 403:
                if "api key" in message.lower():
                    raise AuthError(message)
                raise NotEntitled(message or f"{path} is not included in your plan")
            if resp.status_code == 429:
                self.limiter.cooldown(COOLDOWN_AFTER_429)
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
                self.limiter.sleep(5 * 3**attempt)
                continue
            raise JQuantsError(f"{resp.status_code} on {path} {params}: {message}")
        raise JQuantsError(f"Gave up on {path} {params} after {self.max_retries} retries")

    def get_all(self, path: str, **params: Any) -> list[dict]:
        """GET every page of an endpoint and return the combined `data` rows."""
        params = {k: v for k, v in params.items() if v is not None}
        rows: list[dict] = []
        while True:
            body = self._request(path, params)
            rows.extend(body.get("data", []))
            key = body.get("pagination_key")
            if not key:
                return rows
            params = {**params, "pagination_key": key}


def _message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", ""))
    except ValueError:
        return resp.text[:200]
