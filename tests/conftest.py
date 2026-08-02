from __future__ import annotations

from pathlib import Path

import pytest

from pmwatch.models import BookSide, BookSnapshot, MatchedPair, parse_ts
from pmwatch.venues.fixture import FixtureVenue

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

FED_PAIR = "fed-cut-sep-2026"
REC_PAIR = "us-recession-2026"

PM_FED = "71421073552824748158727123880192916231126298749016433804985512678244506118802"
KAL_FED = "KXFEDCUT-26SEP"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    assert FIXTURES_DIR.is_dir(), "bundled fixtures missing"
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def fixture_venue(fixtures_dir) -> FixtureVenue:
    return FixtureVenue(fixtures_dir)


@pytest.fixture()
def fed_pair() -> MatchedPair:
    return MatchedPair(
        name=FED_PAIR,
        venue_a_id=f"polymarket:{PM_FED}",
        venue_b_id=f"kalshi:{KAL_FED}",
        fee_bps_a=0.0,
        fee_bps_b=50.0,
    )


def make_snapshot(
    venue: str,
    market_id: str,
    ts: str,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> BookSnapshot:
    """Small helper for synthetic books in engine unit tests."""
    return BookSnapshot(
        venue=venue,
        market_id=market_id,
        question="synthetic",
        ts=parse_ts(ts),
        bids=[BookSide(p, s) for p, s in bids],
        asks=[BookSide(p, s) for p, s in asks],
    )
