"""Core data models for pmwatch.

All prices are in dollars, in the range [0, 1], expressed per unit of YES
token unless stated otherwise. Sizes are in contracts/shares as reported by
the venue (no normalization across venues is attempted).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_DEPTH_BAND = 0.02  # 2 cents


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    Accepts a trailing ``Z`` or an explicit offset. Naive inputs are assumed
    to be UTC rather than rejected, because venue payloads are inconsistent.
    """
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_ts(dt: datetime) -> str:
    """Format a datetime as a compact UTC ISO-8601 string (second precision)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class BookSide:
    """One price level of an order book."""

    price: float
    size: float

    def as_pair(self) -> list[float]:
        return [self.price, self.size]


@dataclass
class BookSnapshot:
    """A point-in-time view of one venue's order book for one binary market.

    The book is quoted in terms of the YES token. ``bids`` and ``asks`` are
    sorted on construction (bids descending, asks ascending) so best prices
    are always at index 0 regardless of venue wire order.
    """

    venue: str
    market_id: str
    question: str
    ts: datetime
    bids: list[BookSide] = field(default_factory=list)
    asks: list[BookSide] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=lambda: ["Yes", "No"])

    def __post_init__(self) -> None:
        self.bids.sort(key=lambda s: s.price, reverse=True)
        self.asks.sort(key=lambda s: s.price)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def no_best_ask(self) -> float | None:
        """Best ask of the NO token, derived from the YES best bid.

        In a binary market, buying NO at price ``p`` is identical to selling
        YES at ``1 - p``. The best available NO ask is therefore ``1 - b``
        where ``b`` is the best YES bid. Venues that expose a separate NO
        book (Polymarket does, via a second token id) are still reduced to
        this identity so both legs of a cross-venue comparison share units.
        """
        if self.best_bid is None:
            return None
        return 1.0 - self.best_bid

    def depth_within(self, band: float = DEFAULT_DEPTH_BAND) -> tuple[float, float]:
        """Total size within ``band`` dollars of the best price, per side.

        Returns ``(bid_depth, ask_depth)``. This is a crude but portable
        measure of how much size a market order could sweep near the touch.
        """
        bid_depth = 0.0
        if self.best_bid is not None:
            floor = self.best_bid - band
            bid_depth = sum(s.size for s in self.bids if s.price >= floor)
        ask_depth = 0.0
        if self.best_ask is not None:
            cap = self.best_ask + band
            ask_depth = sum(s.size for s in self.asks if s.price <= cap)
        return bid_depth, ask_depth

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "market_id": self.market_id,
            "question": self.question,
            "ts": format_ts(self.ts),
            "bids": [s.as_pair() for s in self.bids],
            "asks": [s.as_pair() for s in self.asks],
            "outcomes": list(self.outcomes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BookSnapshot":
        return cls(
            venue=data["venue"],
            market_id=data["market_id"],
            question=data.get("question", ""),
            ts=parse_ts(data["ts"]),
            bids=[BookSide(float(p), float(s)) for p, s in data.get("bids", [])],
            asks=[BookSide(float(p), float(s)) for p, s in data.get("asks", [])],
            outcomes=list(data.get("outcomes", ["Yes", "No"])),
        )


KALSHI_FEE_COEFF = 0.07

FEE_MODELS = ("flat_bps", "kalshi")


@dataclass(frozen=True)
class FeeModel:
    """Per-unit taker fee for one leg of a trade, in dollars.

    ``flat_bps`` charges ``price * bps / 10_000``: a linear approximation,
    correct for a venue with a flat proportional taker fee, and correct at
    ``bps=0`` for Polymarket, which charges no taker fee on these markets.

    ``kalshi`` charges ``coeff * price * (1 - price)``, which is Kalshi's
    published schedule (``0.07 * C * P * (1 - P)``, rounded up to the cent
    per order; the rounding is not modeled here because it depends on order
    size, so this is a slight *under*estimate). The quadratic peaks at
    1.75c per contract near a price of 0.50 and falls toward zero at both
    extremes. A flat-bps stand-in therefore understates the true fee badly
    mid-book: at a price of 0.455, 50bps charges 0.23c against a real
    1.74c, which is enough to manufacture a phantom 1.5c arb.
    """

    model: str = "flat_bps"
    bps: float = 0.0
    coeff: float = KALSHI_FEE_COEFF

    def __post_init__(self) -> None:
        if self.model not in FEE_MODELS:
            raise ValueError(
                f"unknown fee model {self.model!r}; expected one of "
                f"{', '.join(FEE_MODELS)}"
            )

    def per_unit(self, price: float) -> float:
        if self.model == "kalshi":
            return self.coeff * price * (1.0 - price)
        return price * self.bps / 10_000.0


@dataclass
class MatchedPair:
    """Two markets on different venues that resolve on the same event.

    ``venue_a_id`` / ``venue_b_id`` carry the venue prefix so a pair is
    self-describing, e.g. ``polymarket:7142...`` or ``kalshi:KXFEDCUT-26SEP``.

    Fees are per-leg and are configuration, not venue truth. ``fee_model_*``
    selects the shape (see :class:`FeeModel`); ``fee_bps_*`` parameterises the
    ``flat_bps`` shape and is ignored by the ``kalshi`` shape.
    """

    name: str
    venue_a_id: str
    venue_b_id: str
    fee_bps_a: float = 0.0
    fee_bps_b: float = 0.0
    question: str = ""
    fee_model_a: str = "flat_bps"
    fee_model_b: str = "flat_bps"

    @property
    def fee_a(self) -> FeeModel:
        return FeeModel(self.fee_model_a, self.fee_bps_a)

    @property
    def fee_b(self) -> FeeModel:
        return FeeModel(self.fee_model_b, self.fee_bps_b)

    @staticmethod
    def split_id(qualified_id: str) -> tuple[str, str]:
        """Split ``"venue:market_id"`` into ``(venue, market_id)``."""
        venue, sep, market_id = qualified_id.partition(":")
        if not sep or not venue or not market_id:
            raise ValueError(
                f"expected 'venue:market_id', got {qualified_id!r}"
            )
        return venue, market_id

    @property
    def venue_a(self) -> str:
        return self.split_id(self.venue_a_id)[0]

    @property
    def venue_b(self) -> str:
        return self.split_id(self.venue_b_id)[0]

    @property
    def market_id_a(self) -> str:
        return self.split_id(self.venue_a_id)[1]

    @property
    def market_id_b(self) -> str:
        return self.split_id(self.venue_b_id)[1]


@dataclass
class Dislocation:
    """An aggregated episode of cross-venue mispricing.

    ``edge`` is the maximum edge (in dollars per unit) observed over the
    episode. ``detected_ts`` is the first snapshot of the streak that opened
    the episode (not the snapshot at which persistence was satisfied);
    ``count`` is the total number of consecutive snapshots in the episode.
    ``details`` carries the price legs and any engine-specific metadata.
    """

    pair: str
    kind: str  # "arb" | "divergence"
    edge: float
    detected_ts: datetime
    last_seen_ts: datetime
    count: int
    details: dict = field(default_factory=dict)

    @property
    def first_seen(self) -> datetime:
        return self.detected_ts

    @property
    def direction(self) -> str:
        return str(self.details.get("direction", ""))
