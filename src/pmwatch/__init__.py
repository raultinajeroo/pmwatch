"""pmwatch — cross-venue prediction-market microstructure monitor.

Read-only research instrument: it snapshots order books across venues,
detects cross-venue dislocations, stores them, and writes daily digests.
It never places orders and nothing it produces is trading advice.
"""

from .detect import DislocationEngine, arb_edge, book_stats, compute_edges, leg_fees
from .match import PairConfigError, load_pairs
from .models import BookSide, BookSnapshot, Dislocation, MatchedPair
from .store import Store
from .venues.base import VenueClient, VenueError
from .venues.fixture import FixtureError, FixtureVenue

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "BookSide",
    "BookSnapshot",
    "Dislocation",
    "MatchedPair",
    "DislocationEngine",
    "arb_edge",
    "compute_edges",
    "leg_fees",
    "book_stats",
    "load_pairs",
    "PairConfigError",
    "Store",
    "VenueClient",
    "VenueError",
    "FixtureVenue",
    "FixtureError",
]
