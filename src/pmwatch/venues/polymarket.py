"""Polymarket venue client (read-only).

Two APIs are involved:

- Gamma (``https://gamma-api.polymarket.com``): market metadata.
  ``GET /markets?clob_token_ids=<id>`` returns a list whose items include
  ``question``, ``outcomes`` (JSON-encoded list, e.g. ``"[\\"Yes\\", \\"No\\"]"``),
  ``outcomePrices`` (same encoding), ``clobTokenIds`` (same encoding) and
  ``volume``.
- CLOB (``https://clob.polymarket.com``): order books per token id.
  ``GET /book?token_id=<id>`` returns
  ``{"market": ..., "asset_id": ..., "timestamp": <ms epoch str>,
    "bids": [{"price": "0.55", "size": "100"}, ...], "asks": [...]}``.

Books are per outcome token. pmwatch works in YES terms; the NO ask is
derived from the YES bid (see :meth:`BookSnapshot.no_best_ask`), so one
token id per market is sufficient.

Reference: https://docs.polymarket.com
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from ..models import BookSide, BookSnapshot
from .base import VenueClient, VenueError

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def _parse_json_list(value: object) -> list:
    """Gamma encodes list fields as JSON strings; accept both forms."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


class PolymarketClient(VenueClient):
    """httpx-backed client with a timeout and exactly one retry per request."""

    venue = "polymarket"

    def __init__(
        self,
        gamma_base: str = GAMMA_BASE,
        clob_base: str = CLOB_BASE,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        self._market_cache: dict[str, dict] = {}

    def _get_json(self, url: str, params: dict | None = None) -> object:
        last_exc: Exception | None = None
        for attempt in (1, 2):  # one retry
            try:
                resp = self._client.get(url, params=params)
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                # Retry only transport errors and 5xx; 4xx is a client bug.
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    break
                if attempt == 1:
                    continue
        raise VenueError(
            f"polymarket request failed: GET {url} params={params}: {last_exc}"
        ) from last_exc

    def list_markets(self, limit: int = 25) -> list[dict]:
        data = self._get_json(
            f"{self.gamma_base}/markets",
            params={"limit": limit, "active": "true", "closed": "false"},
        )
        if not isinstance(data, list):
            raise VenueError(f"polymarket gamma /markets: unexpected payload {type(data)}")
        return data

    def _market_meta(self, token_id: str) -> dict:
        if token_id in self._market_cache:
            return self._market_cache[token_id]
        data = self._get_json(
            f"{self.gamma_base}/markets", params={"clob_token_ids": token_id}
        )
        if not isinstance(data, list) or not data:
            raise VenueError(
                f"polymarket: no gamma market found for clob token id {token_id!r}"
            )
        market = data[0]
        # clobTokenIds are [yes_token, no_token] for binary markets; make sure
        # the id we were given really is one of them before caching.
        token_ids = [str(t) for t in _parse_json_list(market.get("clobTokenIds"))]
        if token_ids and str(token_id) not in token_ids:
            raise VenueError(
                f"polymarket: token {token_id!r} not in market clobTokenIds {token_ids}"
            )
        meta = {
            "question": market.get("question", ""),
            "outcomes": [str(o) for o in _parse_json_list(market.get("outcomes"))],
            "volume": market.get("volume"),
        }
        self._market_cache[token_id] = meta
        return meta

    def get_book(self, market_id: str) -> BookSnapshot:
        """``market_id`` is a Polymarket CLOB token id (the YES token)."""
        data = self._get_json(f"{self.clob_base}/book", params={"token_id": market_id})
        if not isinstance(data, dict) or "bids" not in data or "asks" not in data:
            raise VenueError(
                f"polymarket clob /book for token {market_id!r}: unexpected payload"
            )
        try:
            bids = [BookSide(float(l["price"]), float(l["size"])) for l in data["bids"]]
            asks = [BookSide(float(l["price"]), float(l["size"])) for l in data["asks"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError(
                f"polymarket clob /book for token {market_id!r}: bad level shape: {exc}"
            ) from exc

        raw_ts = data.get("timestamp")
        try:
            ts = datetime.fromtimestamp(int(raw_ts) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError):
            ts = datetime.now(tz=timezone.utc)

        meta = self._market_meta(market_id)
        return BookSnapshot(
            venue=self.venue,
            market_id=market_id,
            question=meta["question"],
            ts=ts,
            bids=bids,
            asks=asks,
            outcomes=meta["outcomes"] or ["Yes", "No"],
        )
