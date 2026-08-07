"""Kalshi venue client (read-only, live mode only).

Live API: ``https://api.elections.kalshi.com/trade-api/v2``.

- ``GET /markets?limit=...`` lists markets (items include ``ticker`` and
  ``title``).
- ``GET /markets/{ticker}/orderbook`` returns bids only. Two payload shapes
  are accepted, because Kalshi migrated to a dollars representation:
  ``{"orderbook_fp": {"yes_dollars": [[price_dollars, size], ...],
                      "no_dollars":  [[price_dollars, size], ...]}}``
  (current; prices and sizes are decimal *strings*), and the older
  ``{"orderbook": {"yes": [[price_cents, size], ...], "no": [...]}}``.
  The YES ask side is implied by the NO bids: a NO bid at price ``p`` is a
  YES ask at ``1 - p``.

Authentication: Kalshi signs requests with an RSA private key
(``KALSHI-ACCESS-KEY`` / ``KALSHI-ACCESS-TIMESTAMP`` /
``KALSHI-ACCESS-SIGNATURE`` headers, RSA-PSS over
``f"{timestamp}{method}{path}"``). pmwatch never signs trades, but read
endpoints on the v2 API still require the signed headers, so the live client
needs the ``cryptography`` package at runtime.

Offline runs do not use this module at all: fixture mode replays fixtures
via ``pmwatch replay`` and demo mode drives the collection path from
fixtures via ``pmwatch demo``. Credential validation lives in
``pmwatch.credentials``.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timezone

import httpx

from ..models import BookSide, BookSnapshot
from .base import VenueClient, VenueError

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient(VenueClient):
    """Signed, read-only Kalshi v2 client."""

    venue = "kalshi"

    def __init__(
        self,
        api_key: str,
        private_key_pem: str,
        base: str = KALSHI_BASE,
        timeout: float = 10.0,
        min_interval_s: float = 1.0,
        client: httpx.Client | None = None,
    ) -> None:
        from ..net import RateLimiter

        self.api_key = api_key
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        self._limiter = RateLimiter(min_interval_s)
        self._market_cache: dict[str, dict] = {}
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise VenueError(
                "kalshi live mode requires the 'cryptography' package for "
                "request signing: pip install cryptography"
            ) from exc
        self._padding = padding
        self._hashes = hashes
        try:
            self._key = serialization.load_pem_private_key(
                private_key_pem.encode(), password=None
            )
        except ValueError as exc:
            raise VenueError(f"kalshi: could not load private key PEM: {exc}") from exc

    def _sign_headers(self, method: str, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path}".encode()
        signature = self._key.sign(
            message,
            self._padding.PSS(
                mgf=self._padding.MGF1(self._hashes.SHA256()),
                salt_length=self._padding.PSS.DIGEST_LENGTH,
            ),
            self._hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    def _get_json(self, path: str, params: dict | None = None) -> object:
        from ..net import get_json_with_backoff

        headers = self._sign_headers("GET", path)
        return get_json_with_backoff(
            self._client,
            f"{self.base}{path}",
            venue="kalshi",
            params=params,
            headers=headers,
            limiter=self._limiter,
        )

    def list_markets(self, limit: int = 25) -> list[dict]:
        data = self._get_json("/markets", params={"limit": limit, "status": "open"})
        if not isinstance(data, dict) or "markets" not in data:
            raise VenueError("kalshi /markets: unexpected payload")
        return data["markets"]

    def _market_meta(self, ticker: str) -> dict:
        if ticker not in self._market_cache:
            data = self._get_json(f"/markets/{ticker}")
            if not isinstance(data, dict) or "market" not in data:
                raise VenueError(f"kalshi: no market found for ticker {ticker!r}")
            market = data["market"]
            self._market_cache[ticker] = {
                "question": market.get("title", ""),
                "outcomes": ["Yes", "No"],
            }
        return self._market_cache[ticker]

    def get_book(self, market_id: str) -> BookSnapshot:
        """``market_id`` is a Kalshi market ticker, e.g. ``KXFEDCUT-26SEP``."""
        data = self._get_json(f"/markets/{market_id}/orderbook")
        if not isinstance(data, dict):
            raise VenueError(f"kalshi orderbook for {market_id!r}: unexpected payload")
        if "orderbook_fp" in data:
            book = data["orderbook_fp"] or {}
            yes_bids = book.get("yes_dollars") or []
            no_bids = book.get("no_dollars") or []
            scale = 1.0
        elif "orderbook" in data:
            book = data["orderbook"] or {}
            yes_bids = book.get("yes") or []
            no_bids = book.get("no") or []
            scale = 0.01
        else:
            raise VenueError(f"kalshi orderbook for {market_id!r}: unexpected payload")
        try:
            bids = [BookSide(float(px) * scale, float(sz)) for px, sz in yes_bids]
            # NO bid at price p == YES ask at (1 - p).
            asks = [BookSide(1.0 - float(px) * scale, float(sz)) for px, sz in no_bids]
        except (TypeError, ValueError) as exc:
            raise VenueError(
                f"kalshi orderbook for {market_id!r}: bad level shape: {exc}"
            ) from exc
        meta = self._market_meta(market_id)
        return BookSnapshot(
            venue=self.venue,
            market_id=market_id,
            question=meta["question"],
            ts=datetime.now(tz=timezone.utc),
            bids=bids,
            asks=asks,
            outcomes=meta["outcomes"],
        )
