"""Replay: run the full detection pipeline over fixture snapshots, in time order.

The fixture root is expected to contain one subdirectory per matched pair,
each with a ``pair.yaml`` (matched-pair metadata, including fees) and one
JSON file per venue market. This keeps ``pmwatch replay --fixtures fixtures/``
self-contained: no separate pairs config is required, and the fees used in
detection are the ones recorded with the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .detect import DislocationEngine
from .match import load_pairs
from .models import Dislocation, MatchedPair
from .store import Store
from .venues.fixture import FixtureError, FixtureVenue


@dataclass
class ReplayResult:
    pairs: list[MatchedPair] = field(default_factory=list)
    dislocations: list[Dislocation] = field(default_factory=list)
    snapshots_stored: int = 0
    timesteps: int = 0
    gaps: list[str] = field(default_factory=list)


def discover_pairs(fixtures_dir: str | Path) -> list[MatchedPair]:
    """Load every ``pair.yaml`` under the fixture root."""
    root = Path(fixtures_dir)
    pair_files = sorted(root.rglob("pair.yaml"))
    if not pair_files:
        raise FixtureError(f"no pair.yaml files found under {root}")
    pairs: list[MatchedPair] = []
    for path in pair_files:
        pairs.extend(load_pairs(path))
    return pairs


def run_replay(
    fixtures_dir: str | Path,
    db_path: str | Path,
    *,
    min_edge: float = 0.01,
    close_edge: float = 0.005,
    min_persistence: int = 3,
    divergence_threshold: float = 0.03,
    store: Store | None = None,
) -> ReplayResult:
    """Replay fixtures through the detector and persist results.

    Pass an existing ``store`` to keep ownership of the connection (used by
    tests); otherwise a Store is opened at ``db_path`` and closed on return.
    """
    venue = FixtureVenue(fixtures_dir)
    pairs = discover_pairs(fixtures_dir)
    engine = DislocationEngine(
        min_edge=min_edge,
        close_edge=close_edge,
        min_persistence=min_persistence,
        divergence_threshold=divergence_threshold,
    )
    result = ReplayResult(pairs=pairs)

    own_store = store is None
    store = store or Store(db_path)
    try:
        store.set_meta("source", "fixtures")
        store.set_meta("fixtures_dir", str(fixtures_dir))

        for pair in pairs:
            ts_a = venue.timestamps(pair.market_id_a)
            ts_b = venue.timestamps(pair.market_id_b)
            timeline = sorted(set(ts_a) | set(ts_b))
            for ts in timeline:
                result.timesteps += 1
                if ts not in ts_a or ts not in ts_b:
                    result.gaps.append(
                        f"{pair.name}: missing one venue snapshot at {ts}"
                    )
                    continue
                snap_a = venue.get_book_at(pair.market_id_a, ts)
                snap_b = venue.get_book_at(pair.market_id_b, ts)
                store.upsert_snapshot(snap_a, source="fixtures")
                store.upsert_snapshot(snap_b, source="fixtures")
                result.snapshots_stored += 2
                closed = engine.process(pair, snap_a, snap_b)
                for d in closed:
                    store.upsert_dislocation(d, source="fixtures")
                    result.dislocations.append(d)
        # Flush episodes still open at the end of the replay window; they are
        # marked open_at_end=True in their details by finalize().
        for d in engine.finalize():
            store.upsert_dislocation(d, source="fixtures")
            result.dislocations.append(d)
    finally:
        if own_store:
            store.close()
    return result
