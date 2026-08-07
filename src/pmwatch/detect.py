"""Cross-venue dislocation detection.

Detection math
--------------
For a matched binary pair (venue A, venue B), let

- ``aA`` = best ask of the YES token on venue A
- ``nB`` = best ask of the NO token on venue B (derived as ``1 - best YES bid``)

Buying YES on A and NO on B pays exactly $1 in every state of the world, so
the per-unit edge after fees is::

    edge_AB = 1 - aA - nB - fees
    fees    = fee_a.per_unit(aA) + fee_b.per_unit(nB)

The symmetric direction (YES on B + NO on A) gives ``edge_BA``.

Each leg carries its own fee model (see :class:`pmwatch.models.FeeModel`).
``flat_bps`` is linear in price; ``kalshi`` is the venue's published
``0.07 * P * (1 - P)`` quadratic, which peaks mid-book. Using a flat
approximation where the venue charges the quadratic manufactures phantom
arbs at mid-range prices, so the model matters as much as the parameter.

Episode rules
-------------
An arb episode *opens* when edge >= ``min_edge`` for at least
``min_persistence`` consecutive snapshots, and *stays open* (hysteresis)
until edge < ``close_edge``. This avoids flapping when a marginal signal
straddles the threshold.

A divergence episode tracks ``|mid_A - mid_B| >= divergence_threshold`` with
the same persistence rule and a close threshold of half the open threshold.
Divergence is informational: two venues can disagree without a tradeable
cross, because each book has its own spread.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import BookSnapshot, Dislocation, FeeModel, MatchedPair

KIND_ARB = "arb"
KIND_DIVERGENCE = "divergence"


def _as_fee(fee: FeeModel | float) -> FeeModel:
    """Coerce a fee argument. A bare number means flat basis points, which is
    what these functions accepted before fee models existed."""
    return fee if isinstance(fee, FeeModel) else FeeModel(bps=float(fee))


def leg_fees(
    yes_price: float,
    fee_yes: FeeModel | float,
    no_price: float,
    fee_no: FeeModel | float,
) -> float:
    """Taker fees for a YES leg and a NO leg, in dollars per unit."""
    return _as_fee(fee_yes).per_unit(yes_price) + _as_fee(fee_no).per_unit(no_price)


def arb_edge(
    yes_ask: float,
    fee_yes_venue: FeeModel | float,
    no_ask: float,
    fee_no_venue: FeeModel | float,
) -> float:
    """Edge from buying YES at ``yes_ask`` on one venue and NO at ``no_ask``
    on the other, after fees. Positive means the two legs cost less than $1.
    """
    return 1.0 - yes_ask - no_ask - leg_fees(
        yes_ask, fee_yes_venue, no_ask, fee_no_venue
    )


def compute_edges(pair: MatchedPair, snap_a: BookSnapshot, snap_b: BookSnapshot) -> dict:
    """Both arb directions plus mid divergence for one pair at one timestep.

    Returns a dict with ``edge_ab``, ``edge_ba``, ``divergence`` and the raw
    legs for transparency. Requires non-empty books on both venues.
    """
    a_yes_ask = snap_a.best_ask
    b_yes_ask = snap_b.best_ask
    a_no_ask = snap_a.no_best_ask
    b_no_ask = snap_b.no_best_ask
    if None in (a_yes_ask, b_yes_ask, a_no_ask, b_no_ask):
        raise ValueError("cannot compute edges on an empty book")

    edge_ab = arb_edge(a_yes_ask, pair.fee_a, b_no_ask, pair.fee_b)
    edge_ba = arb_edge(b_yes_ask, pair.fee_b, a_no_ask, pair.fee_a)
    divergence = abs(snap_a.mid - snap_b.mid)  # type: ignore[operator]

    return {
        "edge_ab": edge_ab,
        "edge_ba": edge_ba,
        "divergence": divergence,
        "legs": {
            "a_yes_ask": a_yes_ask,
            "b_yes_ask": b_yes_ask,
            "a_no_ask": a_no_ask,
            "b_no_ask": b_no_ask,
        },
    }


def book_stats(snap: BookSnapshot, band: float = 0.02) -> dict:
    """Summary statistics for one snapshot (used by the store and digest)."""
    bid_depth, ask_depth = snap.depth_within(band)
    return {
        "best_bid": snap.best_bid,
        "best_ask": snap.best_ask,
        "mid": snap.mid,
        "spread": snap.spread,
        "bid_depth_2c": bid_depth,
        "ask_depth_2c": ask_depth,
    }


@dataclass
class _Episode:
    """State machine for one (pair, kind, direction) signal."""

    open_threshold: float
    close_threshold: float
    min_persistence: int
    streak: int = 0
    streak_first_ts: datetime | None = None
    streak_max: float = float("-inf")
    is_open: bool = False
    detected_ts: datetime | None = None
    last_seen_ts: datetime | None = None
    count: int = 0
    max_edge: float = float("-inf")

    def update(self, value: float, ts: datetime) -> bool:
        """Feed one observation. Returns True if the episode closed here."""
        if value >= self.open_threshold:
            if self.streak == 0:
                self.streak_first_ts = ts
                self.streak_max = value
            else:
                self.streak_max = max(self.streak_max, value)
            self.streak += 1
        else:
            self.streak = 0
            self.streak_first_ts = None
            self.streak_max = float("-inf")

        if not self.is_open:
            if self.streak >= self.min_persistence:
                # Open the episode; the persistence ramp counts toward it.
                self.is_open = True
                self.detected_ts = self.streak_first_ts
                self.last_seen_ts = ts
                self.count = self.streak
                self.max_edge = self.streak_max
            return False

        # Episode is open: hysteresis keeps it alive down to close_threshold.
        if value >= self.close_threshold:
            self.count += 1
            self.last_seen_ts = ts
            self.max_edge = max(self.max_edge, value)
            return False
        return True

    def to_dislocation(self, pair: str, kind: str, details: dict) -> Dislocation:
        assert self.detected_ts is not None and self.last_seen_ts is not None
        return Dislocation(
            pair=pair,
            kind=kind,
            edge=self.max_edge,
            detected_ts=self.detected_ts,
            last_seen_ts=self.last_seen_ts,
            count=self.count,
            details=details,
        )


class DislocationEngine:
    """Stateful detector; feed it matched snapshots in time order."""

    def __init__(
        self,
        min_edge: float = 0.01,
        close_edge: float = 0.005,
        min_persistence: int = 3,
        divergence_threshold: float = 0.03,
    ) -> None:
        if close_edge >= min_edge:
            raise ValueError("close_edge must be below min_edge for hysteresis")
        self.min_edge = min_edge
        self.close_edge = close_edge
        self.min_persistence = min_persistence
        self.divergence_threshold = divergence_threshold
        self._episodes: dict[tuple[str, str, str], _Episode] = {}
        self._details: dict[tuple[str, str, str], dict] = {}

    def _episode(self, key: tuple[str, str, str]) -> _Episode:
        if key not in self._episodes:
            _, kind, _ = key
            if kind == KIND_ARB:
                ep = _Episode(self.min_edge, self.close_edge, self.min_persistence)
            else:
                ep = _Episode(
                    self.divergence_threshold,
                    self.divergence_threshold / 2.0,
                    self.min_persistence,
                )
            self._episodes[key] = ep
        return self._episodes[key]

    def process(
        self, pair: MatchedPair, snap_a: BookSnapshot, snap_b: BookSnapshot
    ) -> list[Dislocation]:
        """Process one timestep. Returns episodes that closed at this step."""
        ts = max(snap_a.ts, snap_b.ts)
        edges = compute_edges(pair, snap_a, snap_b)
        legs = edges["legs"]
        closed: list[Dislocation] = []

        signals = [
            (
                (pair.name, KIND_ARB, "AB"),
                edges["edge_ab"],
                {
                    "direction": "AB",
                    "buy_yes": pair.venue_a_id,
                    "buy_no": pair.venue_b_id,
                    "yes_ask": legs["a_yes_ask"],
                    "no_ask": legs["b_no_ask"],
                    "fee_model_yes_venue": pair.fee_model_a,
                    "fee_model_no_venue": pair.fee_model_b,
                    "fee_bps_yes_venue": pair.fee_bps_a,
                    "fee_bps_no_venue": pair.fee_bps_b,
                    "fees": leg_fees(
                        legs["a_yes_ask"], pair.fee_a,
                        legs["b_no_ask"], pair.fee_b,
                    ),
                },
            ),
            (
                (pair.name, KIND_ARB, "BA"),
                edges["edge_ba"],
                {
                    "direction": "BA",
                    "buy_yes": pair.venue_b_id,
                    "buy_no": pair.venue_a_id,
                    "yes_ask": legs["b_yes_ask"],
                    "no_ask": legs["a_no_ask"],
                    "fee_model_yes_venue": pair.fee_model_b,
                    "fee_model_no_venue": pair.fee_model_a,
                    "fee_bps_yes_venue": pair.fee_bps_b,
                    "fee_bps_no_venue": pair.fee_bps_a,
                    "fees": leg_fees(
                        legs["b_yes_ask"], pair.fee_b,
                        legs["a_no_ask"], pair.fee_a,
                    ),
                },
            ),
            (
                (pair.name, KIND_DIVERGENCE, ""),
                edges["divergence"],
                {
                    "mid_a": snap_a.mid,
                    "mid_b": snap_b.mid,
                },
            ),
        ]

        for key, value, details in signals:
            ep = self._episode(key)
            was_open = ep.is_open
            just_closed = ep.update(value, ts)
            if ep.is_open and (not was_open or value >= ep.max_edge):
                self._details[key] = details
            if just_closed:
                closed.append(ep.to_dislocation(pair.name, key[1], self._details.get(key, details)))
                del self._episodes[key]
                self._details.pop(key, None)
        return closed

    def open_episodes(self) -> list[Dislocation]:
        """Snapshot of currently open episodes (does not mutate state)."""
        out = []
        for key, ep in self._episodes.items():
            if ep.is_open:
                out.append(ep.to_dislocation(key[0], key[1], self._details.get(key, {})))
        return out

    def finalize(self) -> list[Dislocation]:
        """Close every open episode (e.g. at end of a replay window).

        Records are marked with ``details["open_at_end"] = True`` so reports
        can distinguish a real close from a window boundary.
        """
        out = []
        for key, ep in list(self._episodes.items()):
            if ep.is_open:
                details = dict(self._details.get(key, {}))
                details["open_at_end"] = True
                out.append(ep.to_dislocation(key[0], key[1], details))
        self._episodes.clear()
        self._details.clear()
        return out
