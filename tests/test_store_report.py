"""Store roundtrip, report smoke test, and fixture venue behavior."""

from __future__ import annotations

import json

import pytest

from pmwatch.detect import book_stats
from pmwatch.models import BookSnapshot, Dislocation, parse_ts
from pmwatch.replay import run_replay
from pmwatch.report import generate_report
from pmwatch.store import Store
from pmwatch.venues.fixture import FixtureError
from conftest import make_snapshot


def test_snapshot_roundtrip(tmp_path):
    with Store(tmp_path / "t.db") as store:
        snap = make_snapshot(
            "polymarket", "m1", "2026-07-30T14:00:00Z",
            bids=[(0.568, 820.0), (0.566, 1450.0), (0.561, 2600.0), (0.545, 4100.0)],
            asks=[(0.575, 640.0), (0.577, 1100.0), (0.581, 2300.0), (0.597, 3900.0)],
        )
        store.upsert_snapshot(snap, source="fixtures")
        rows = store.snapshots_for_date("2026-07-30")
        assert len(rows) == 1
        row = rows[0]
        assert row["venue"] == "polymarket"
        assert row["market_id"] == "m1"
        assert row["best_bid"] == pytest.approx(0.568)
        assert row["best_ask"] == pytest.approx(0.575)
        assert row["mid"] == pytest.approx(0.5715)
        assert row["spread"] == pytest.approx(0.007)
        # Depth within 2c of touch: three levels per side; the fourth level
        # (0.545 bid / 0.597 ask) sits outside the band.
        stats = book_stats(snap)
        assert stats["bid_depth_2c"] == pytest.approx(820 + 1450 + 2600)
        assert stats["ask_depth_2c"] == pytest.approx(640 + 1100 + 2300)
        assert row["bid_depth_2c"] == pytest.approx(stats["bid_depth_2c"])
        # Full book survives as JSON.
        book = json.loads(row["book_json"])
        restored = BookSnapshot.from_dict(book)
        assert restored.bids == snap.bids and restored.asks == snap.asks
        assert restored.ts == snap.ts
        # Upsert is idempotent on (ts, venue, market_id).
        store.upsert_snapshot(snap, source="fixtures")
        assert len(store.snapshots_for_date("2026-07-30")) == 1


def test_dislocation_roundtrip(tmp_path):
    d = Dislocation(
        pair="fed-cut-sep-2026",
        kind="arb",
        edge=0.023,
        detected_ts=parse_ts("2026-07-30T14:00:00Z"),
        last_seen_ts=parse_ts("2026-07-30T14:25:00Z"),
        count=6,
        details={"direction": "AB", "yes_ask": 0.575, "no_ask": 0.4, "fees": 0.002},
    )
    with Store(tmp_path / "t.db") as store:
        store.upsert_dislocation(d, source="fixtures")
        store.upsert_dislocation(d, source="fixtures")  # idempotent
        rows = store.dislocations_for_date("2026-07-30")
        assert len(rows) == 1
        row = rows[0]
        assert row["pair"] == "fed-cut-sep-2026"
        assert row["kind"] == "arb"
        assert row["direction"] == "AB"
        assert row["max_edge"] == pytest.approx(0.023)
        assert row["count"] == 6
        assert json.loads(row["details_json"])["yes_ask"] == pytest.approx(0.575)
        assert store.dislocations_for_date("2026-07-31") == []


def test_report_smoke_after_replay(tmp_path, fixtures_dir):
    db = tmp_path / "replay.db"
    run_replay(fixtures_dir, db)
    with Store(db) as store:
        text = generate_report(store, "2026-07-30")
    # Pair name, exact planted edge, both episode kinds.
    assert "fed-cut-sep-2026" in text
    assert "+0.0230" in text
    assert "arb" in text and "divergence" in text
    # Required sections.
    assert "## Top dislocations" in text
    assert "## Book statistics" in text
    assert "## Data quality" in text
    # Honest provenance + disclaimer.
    assert "Generated from bundled fixtures" in text
    assert "not" in text.lower() and "trading advice" in text
    # Arb rows sort ahead of divergence rows; clean fixture cadence shows
    # no collection gaps.
    assert text.index("| fed-cut-sep-2026 | arb |") < text.index(
        "| fed-cut-sep-2026 | divergence |"
    )
    assert "| none |" in text


def test_fixture_venue_refuses_unknown_market(fixture_venue):
    with pytest.raises(FixtureError, match="unknown market_id"):
        fixture_venue.get_book("definitely-not-a-market")
    with pytest.raises(FixtureError, match="unknown market_id"):
        fixture_venue.get_book_at("definitely-not-a-market", "2026-07-30T14:00:00Z")


def test_fixture_venue_refuses_unknown_timestamp(fixture_venue, fed_pair):
    with pytest.raises(FixtureError, match="no fixture snapshot"):
        fixture_venue.get_book_at(fed_pair.market_id_a, "2026-07-30T15:00:00Z")


def test_fixture_root_missing(tmp_path):
    with pytest.raises(FixtureError, match="not found"):
        from pmwatch.venues.fixture import FixtureVenue

        FixtureVenue(tmp_path / "nope")
