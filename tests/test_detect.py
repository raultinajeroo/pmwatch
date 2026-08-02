"""Detection engine tests: planted fixture arb, hysteresis, fees, divergence."""

from __future__ import annotations

import pytest

from pmwatch.detect import (
    DislocationEngine,
    arb_edge,
    compute_edges,
    leg_fees,
)
from pmwatch.models import parse_ts
from conftest import make_snapshot

# Exact planted math for fixtures/fed-cut-sep-2026, snapshots 1-6:
# buy YES on polymarket @ 0.575 (fee 0 bps) + buy NO on kalshi @ 0.400
# (fee 50 bps) -> edge = 1 - 0.575 - 0.400 - 0.002 = 0.023.
PLANTED_YES_ASK_A = 0.575
PLANTED_NO_ASK_B = 0.400
PLANTED_FEES = 0.002
PLANTED_EDGE = 1 - PLANTED_YES_ASK_A - PLANTED_NO_ASK_B - PLANTED_FEES


def test_fee_math_proportional_both_legs():
    # fees = (p_yes * bps_yes + p_no * bps_no) / 10_000
    assert leg_fees(0.575, 0.0, 0.400, 50.0) == pytest.approx(0.002, abs=1e-12)
    assert leg_fees(0.60, 25.0, 0.30, 100.0) == pytest.approx(
        (0.60 * 25 + 0.30 * 100) / 10_000, abs=1e-12
    )
    # zero fees -> zero cost
    assert leg_fees(0.55, 0.0, 0.42, 0.0) == 0.0


def test_arb_edge_formula():
    edge = arb_edge(PLANTED_YES_ASK_A, 0.0, PLANTED_NO_ASK_B, 50.0)
    assert edge == pytest.approx(0.023, abs=1e-12)
    # crossed books with no fees give exactly the gross edge
    assert arb_edge(0.50, 0.0, 0.45, 0.0) == pytest.approx(0.05, abs=1e-12)


def test_planted_arb_detected_with_exact_edge(fed_pair, fixture_venue):
    engine = DislocationEngine()
    closed = []
    open_after_step = {}
    ts_list = fixture_venue.timestamps(fed_pair.market_id_a)
    assert len(ts_list) == 12
    for i, ts in enumerate(ts_list, start=1):
        snap_a = fixture_venue.get_book_at(fed_pair.market_id_a, ts)
        snap_b = fixture_venue.get_book_at(fed_pair.market_id_b, ts)
        closed.extend(engine.process(fed_pair, snap_a, snap_b))
        open_after_step[i] = engine.open_episodes()
    closed.extend(engine.finalize())

    # Persistence: not open after snapshot 1, open by snapshot 3.
    assert not any(d.kind == "arb" for d in open_after_step[1])
    assert any(d.kind == "arb" for d in open_after_step[3])

    arbs = [d for d in closed if d.kind == "arb"]
    assert len(arbs) == 1
    arb = arbs[0]
    assert arb.pair == fed_pair.name
    assert arb.direction == "AB"
    assert arb.edge == pytest.approx(PLANTED_EDGE, abs=1e-6)
    assert arb.edge == pytest.approx(0.023, abs=1e-6)
    # Episode spans the six planted snapshots (count includes the ramp).
    assert arb.count == 6
    assert arb.detected_ts == parse_ts("2026-07-30T14:00:00Z")
    assert arb.last_seen_ts == parse_ts("2026-07-30T14:25:00Z")
    # Legs are recorded for auditability.
    assert arb.details["yes_ask"] == pytest.approx(PLANTED_YES_ASK_A, abs=1e-9)
    assert arb.details["no_ask"] == pytest.approx(PLANTED_NO_ASK_B, abs=1e-9)
    assert arb.details["fees"] == pytest.approx(PLANTED_FEES, abs=1e-9)
    # The episode must have genuinely closed (fixtures converge at snapshot 7).
    assert not arb.details.get("open_at_end", False)


def test_aligned_pair_produces_zero_dislocations(fixture_venue):
    from pmwatch.replay import discover_pairs

    pair = next(p for p in discover_pairs(fixture_venue.root) if p.name == "us-recession-2026")
    engine = DislocationEngine()
    closed = []
    for ts in fixture_venue.timestamps(pair.market_id_a):
        closed.extend(
            engine.process(
                pair,
                fixture_venue.get_book_at(pair.market_id_a, ts),
                fixture_venue.get_book_at(pair.market_id_b, ts),
            )
        )
    closed.extend(engine.finalize())
    assert closed == []


def _snap_with_prices(venue: str, ts: str, bid: float, ask: float):
    return make_snapshot(venue, "m", ts, [(bid, 100.0)], [(ask, 100.0)])


def test_hysteresis_stays_open_at_0_7c_closes_at_0_4c(fed_pair):
    """Edge sequence: 1.2c x3 (opens), 0.7c (held by hysteresis), 0.4c (closes).

    Engineered via A-ask/B-bid: edge = bB - aA - 0.005 * (1 - bB).
    Choose aA = 0.50 always; pick bB per target edge.
    """
    engine = DislocationEngine()

    def step(ts: str, b_bid: float):
        snap_a = _snap_with_prices("polymarket", ts, 0.49, 0.50)
        snap_b = _snap_with_prices("kalshi", ts, b_bid, min(b_bid + 0.01, 0.99))
        return engine.process(fed_pair, snap_a, snap_b)

    # Solve bB for target edges: edge = 1 - 0.50 - (1-bB) - 0.005*(1-bB)
    # => edge = bB - 0.50 - 0.005 + 0.005*bB = 1.005*bB - 0.505
    def bid_for(edge: float) -> float:
        return (edge + 0.505) / 1.005

    closed = []
    for i in range(3):  # three snapshots at ~1.2c -> opens at step 3
        closed += step(f"2026-08-01T10:0{i}:00Z", bid_for(0.012))
    assert engine.open_episodes(), "episode should be open after 3 qualifying snapshots"

    closed += step("2026-08-01T10:03:00Z", bid_for(0.007))  # 0.7c: inside hysteresis band
    assert not closed, "episode must stay open while edge >= close threshold (0.5c)"
    assert engine.open_episodes()

    closed += step("2026-08-01T10:04:00Z", bid_for(0.004))  # 0.4c: below close threshold
    assert len(closed) == 1, "episode must close once edge < 0.5c"
    # 3 opening snapshots + 1 held by hysteresis; the closing snapshot at
    # 0.4c is not part of the episode.
    assert closed[0].count == 4
    assert closed[0].edge == pytest.approx(0.012, abs=1e-6)
    assert not engine.open_episodes()


def test_divergence_flagged_separately_from_arb(fed_pair):
    """Mids 5c apart but books never cross: divergence, no arb.

    Spreads (8c) exceed the mid gap (5c), so neither arb direction is
    positive: edge_AB = 1 - 0.52 - 0.51 - fees < 0.
    """
    engine = DislocationEngine()
    closed = []
    for i in range(4):
        # A: 0.44/0.52 (mid 0.48); B: 0.49/0.57 (mid 0.53). No cross.
        snap_a = _snap_with_prices("polymarket", f"2026-08-02T09:0{i}:00Z", 0.44, 0.52)
        snap_b = _snap_with_prices("kalshi", f"2026-08-02T09:0{i}:00Z", 0.49, 0.57)
        closed += engine.process(fed_pair, snap_a, snap_b)

    open_kinds = {d.kind for d in engine.open_episodes()}
    assert "divergence" in open_kinds
    assert "arb" not in open_kinds
    closed += engine.finalize()
    kinds = {d.kind for d in closed}
    assert kinds == {"divergence"}
    div = closed[0]
    assert div.edge == pytest.approx(0.05, abs=1e-9)
    assert div.count == 4
    assert div.details.get("open_at_end") is True


def test_compute_edges_uses_fixture_shapes(fed_pair, fixture_venue):
    snap_a = fixture_venue.get_book_at(fed_pair.market_id_a, "2026-07-30T14:00:00Z")
    snap_b = fixture_venue.get_book_at(fed_pair.market_id_b, "2026-07-30T14:00:00Z")
    edges = compute_edges(fed_pair, snap_a, snap_b)
    assert edges["edge_ab"] == pytest.approx(0.023, abs=1e-9)
    assert edges["edge_ba"] < 0
    assert edges["divergence"] == pytest.approx(0.031, abs=1e-9)
