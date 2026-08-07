"""Kalshi order-book parser tests against recorded payload shapes.

Kalshi migrated the order-book endpoint to a dollars representation
(``orderbook_fp`` / ``yes_dollars``, decimal strings). The client accepts both
that and the older integer-cent ``orderbook`` / ``yes`` form. No network and
no credentials: ``_get_json`` is stubbed.
"""

from __future__ import annotations

import pytest

from pmwatch.venues.base import VenueError
from pmwatch.venues.kalshi import KalshiClient

# Recorded live from /markets/KXGOVFLNOMR-26-BD/orderbook on 2026-08-06,
# truncated to the top levels. Note the sub-cent prices, which the older
# integer-cent payload could not represent at all.
RECORDED_FP = {
    "orderbook_fp": {
        "yes_dollars": [["0.0010", "640476.00"], ["0.0100", "32138.74"]],
        "no_dollars": [["0.0010", "446000.00"], ["0.0020", "165000.00"]],
    }
}

# The same book expressed in each shape, at prices integer cents can express.
EQUIV_FP = {
    "orderbook_fp": {
        "yes_dollars": [["0.5500", "100.00"], ["0.5400", "250.00"]],
        "no_dollars": [["0.4000", "300.00"], ["0.3900", "150.00"]],
    }
}
EQUIV_LEGACY = {
    "orderbook": {
        "yes": [[55, 100.0], [54, 250.0]],
        "no": [[40, 300.0], [39, 150.0]],
    }
}


class _StubClient(KalshiClient):
    """KalshiClient with signing and HTTP bypassed."""

    def __init__(self, payload):
        self._payload = payload
        self._market_cache = {"T": {"question": "q", "outcomes": ["Yes", "No"]}}

    def _get_json(self, path, params=None):
        return self._payload


def _levels(sides):
    return [(round(s.price, 6), s.size) for s in sides]


def _book(payload):
    return _StubClient(payload).get_book("T")


@pytest.mark.parametrize("payload", [EQUIV_FP, EQUIV_LEGACY])
def test_dollar_and_cent_payloads_parse_to_the_same_book(payload):
    book = _book(payload)
    assert _levels(book.bids) == [(0.55, 100.0), (0.54, 250.0)]
    # NO bid at p is a YES ask at 1 - p.
    assert _levels(book.asks) == [(0.6, 300.0), (0.61, 150.0)]


def test_recorded_live_payload_parses():
    book = _book(RECORDED_FP)
    assert _levels(book.bids) == [(0.01, 32138.74), (0.001, 640476.0)]
    assert _levels(book.asks) == [(0.998, 165000.0), (0.999, 446000.0)]


def test_missing_orderbook_key_is_a_venue_error():
    with pytest.raises(VenueError, match="unexpected payload"):
        _book({"something_else": {}})


def test_bad_level_shape_is_a_venue_error():
    with pytest.raises(VenueError, match="bad level shape"):
        _book({"orderbook_fp": {"yes_dollars": [["oops", "1"]], "no_dollars": []}})
