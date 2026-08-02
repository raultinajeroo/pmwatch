"""Kalshi venue client (read-only) plus an offline paper mode.

Live API: ``https://api.elections.kalshi.com/trade-api/v2``.

- ``GET /markets?limit=...`` lists markets (items include ``ticker`` and
  ``title``).
- ``GET /markets/{ticker}/orderbook`` returns bids only, in cents:
  ``{"orderbook": {"yes": [[price_cents, size], ...],
                   "no":  [[price_cents, size], ...]}}``.
  The YES ask side is implied by the NO bids: a NO bid at ``c`` cents is a
  YES ask at ``100 - c`` cents.

Authentication: Kalshi signs requests with an RSA private key
(``KALSHI-ACCESS-KEY`` / ``KALSHI-ACCESS-TIMESTAMP`` /
``KALSHI-ACCESS-SIGNATURE`` headers, RSA-PSS over
``f"{timestamp}{method}{path}"``). pmwatch never signs trades, but read
endpoints on the v2 API still require the signed headers, so the live client
needs the ``cryptography`` package at runtime.

If ``KALSHI_API_KEY`` is not set, :func:`make_kalshi_client` returns a
paper-mode client that serves Kalshi-shaped data from the bundled fixtures,
with a one-line warning on stderr. Paper mode keeps ``collect``/``watch``
usable for demos without credentials.
"""

from __future__ import annotations

import base64
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..models import BookSide, BookSnapshot
from .base import VenueClient, VenueError
from .fixture import FixtureVenue

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
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
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
        headers = self._sign_headers("GET", path)
        last_exc: Exception | None = None
        for attempt in (1, 2):  # one retry
            try:
                resp = self._client.get(f"{self.base}{path}", params=params, headers=headers)
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    break
                if attempt == 1:
                    continue
        raise VenueError(f"kalshi request failed: GET {path}: {last_exc}") from last_exc

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
        if not isinstance(data, dict) or "orderbook" not in data:
            raise VenueError(f"kalshi orderbook for {market_id!r}: unexpected payload")
        book = data["orderbook"] or {}
        try:
            yes_bids = book.get("yes") or []
            no_bids = book.get("no") or []
            bids = [BookSide(px / 100.0, float(sz)) for px, sz in yes_bids]
            # NO bid at c cents == YES ask at (100 - c) cents.
            asks = [BookSide((100 - px) / 100.0, float(sz)) for px, sz in no_bids]
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


class KalshiPaperMode(VenueClient):
    """Serves Kalshi-labelled snapshots from fixtures (no credentials needed)."""

    venue = "kalshi"

    def __init__(self, fixtures_dir: str | Path) -> None:
        self._fixture = FixtureVenue(fixtures_dir)

    def list_markets(self) -> list[dict]:
        return [m for m in self._fixture.list_markets() if m["venue"] == "kalshi"]

    def get_book(self, market_id: str) -> BookSnapshot:
        snap = self._fixture.get_book(market_id)
        if snap.venue != "kalshi":
            raise VenueError(
                f"kalshi paper mode: market {market_id!r} is a {snap.venue} fixture"
            )
        return snap


def make_kalshi_client(fixtures_dir: str | Path | None = None, **kwargs) -> VenueClient:
    """Build a live KalshiClient when credentials exist, else paper mode.

    Reads ``KALSHI_API_KEY`` and ``KALSHI_API_SECRET`` (PEM private key) from
    the environment. Without them, returns :class:`KalshiPaperMode` backed by
    ``fixtures_dir`` (default: ``fixtures/`` in the current working directory)
    and prints a loud one-line warning to stderr.
    """
    api_key = os.environ.get("KALSHI_API_KEY")
    api_secret = os.environ.get("KALSHI_API_SECRET", "")
    if api_key:
        return KalshiClient(api_key=api_key, private_key_pem=api_secret, **kwargs)
    root = Path(fixtures_dir) if fixtures_dir else Path("fixtures")
    print(
        "WARNING: KALSHI_API_KEY not set; using Kalshi paper mode "
        f"(fixture data from {root}/, NOT live Kalshi data)",
        file=sys.stderr,
    )
    return KalshiPaperMode(root)
