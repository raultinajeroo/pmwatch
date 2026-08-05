"""Shared HTTP plumbing: per-venue rate limiting and backoff with jitter.

Live venue calls go through :func:`get_json_with_backoff` so every adapter
gets the same behavior: a minimum interval between requests to a venue,
bounded retries on transport errors and 5xx/429 responses, and full jitter
on the backoff so concurrent watchers do not retry in lockstep. 4xx
responses other than 429 are client bugs and are not retried.
"""

from __future__ import annotations

import random
import time

import httpx

from .venues.base import VenueError

DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY_S = 0.5


class RateLimiter:
    """Enforces a minimum interval between requests to one venue."""

    def __init__(self, min_interval_s: float = 1.0) -> None:
        self.min_interval_s = min_interval_s
        self._last: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        remaining = self.min_interval_s - (now - self._last)
        if remaining > 0:
            time.sleep(remaining)
        self._last = time.monotonic()


def get_json_with_backoff(
    client: httpx.Client,
    url: str,
    *,
    venue: str,
    params: dict | None = None,
    headers: dict | None = None,
    limiter: RateLimiter | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    sleep=time.sleep,
) -> object:
    """GET ``url`` with rate limiting and jittered exponential backoff.

    Raises :class:`VenueError` naming the venue, path, and final cause after
    the retries are exhausted. ``sleep`` is injectable for tests.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        if limiter is not None:
            limiter.wait()
        try:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            status = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            retryable = status is None or status == 429 or status >= 500
            if not retryable or attempt == max_retries:
                break
            delay = base_delay_s * (2 ** attempt)
            sleep(random.uniform(0.0, delay))  # full jitter
    raise VenueError(f"{venue} request failed: GET {url}: {last_exc}") from last_exc
