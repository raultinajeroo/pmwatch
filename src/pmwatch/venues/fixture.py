"""Fixture-backed venue: serves recorded snapshots from disk.

This drives the offline replay mode and the test suite. Fixture files are
JSON with the same field names the live adapters produce, e.g.::

    {
      "venue": "polymarket",
      "market_id": "7142...",
      "question": "Fed rate cut at the September 2026 FOMC meeting?",
      "outcomes": ["Yes", "No"],
      "snapshots": [
        {"ts": "2026-07-30T14:00:00Z",
         "bids": [[0.568, 820], ...],
         "asks": [[0.575, 640], ...]}
      ]
    }

The fixture root may contain files at any depth; replay expects one
subdirectory per matched pair but this client does not require it.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import BookSnapshot, parse_ts
from .base import VenueClient, VenueError


class FixtureError(VenueError):
    """Raised for unknown markets, unknown timestamps, or malformed files."""


class FixtureVenue(VenueClient):
    """A VenueClient that replays recorded snapshots from a directory."""

    venue = "fixture"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if not self.root.is_dir():
            raise FixtureError(f"fixture directory not found: {self.root}")
        # market_id -> {"meta": {...}, "snaps": {ts: BookSnapshot}}
        self._markets: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        files = sorted(self.root.rglob("*.json"))
        if not files:
            raise FixtureError(f"no fixture .json files under {self.root}")
        for path in files:
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise FixtureError(f"malformed fixture file {path}: {exc}") from exc
            market_id = data.get("market_id")
            if not market_id or "snapshots" not in data:
                raise FixtureError(
                    f"fixture file {path} missing 'market_id' or 'snapshots'"
                )
            snaps: dict[str, BookSnapshot] = {}
            for raw in data["snapshots"]:
                snap = BookSnapshot.from_dict(
                    {
                        "venue": data["venue"],
                        "market_id": market_id,
                        "question": data.get("question", ""),
                        "outcomes": data.get("outcomes", ["Yes", "No"]),
                        **raw,
                    }
                )
                snaps[raw["ts"]] = snap
            if market_id in self._markets:
                raise FixtureError(f"duplicate fixture for market {market_id}")
            self._markets[market_id] = {
                "meta": {
                    "venue": data["venue"],
                    "market_id": market_id,
                    "question": data.get("question", ""),
                    "outcomes": data.get("outcomes", ["Yes", "No"]),
                    "file": str(path),
                },
                "snaps": snaps,
            }

    def list_markets(self) -> list[dict]:
        out = []
        for entry in self._markets.values():
            meta = dict(entry["meta"])
            ts_sorted = sorted(entry["snaps"])
            meta["n_snapshots"] = len(ts_sorted)
            meta["first_ts"] = ts_sorted[0]
            meta["last_ts"] = ts_sorted[-1]
            out.append(meta)
        return out

    def _entry(self, market_id: str) -> dict:
        if market_id not in self._markets:
            known = ", ".join(sorted(self._markets)) or "<none>"
            raise FixtureError(
                f"unknown market_id {market_id!r} for fixture venue; "
                f"known markets: {known}"
            )
        return self._markets[market_id]

    def timestamps(self, market_id: str) -> list[str]:
        return sorted(self._entry(market_id)["snaps"])

    def get_book_at(self, market_id: str, ts: str) -> BookSnapshot:
        """Snapshot at an exact fixture timestamp (replay interface)."""
        entry = self._entry(market_id)
        if ts not in entry["snaps"]:
            known = ", ".join(sorted(entry["snaps"]))
            raise FixtureError(
                f"no fixture snapshot for {market_id!r} at {ts}; "
                f"available: {known}"
            )
        return entry["snaps"][ts]

    def get_book(self, market_id: str) -> BookSnapshot:
        """Latest recorded snapshot (VenueClient interface)."""
        entry = self._entry(market_id)
        latest_ts = max(entry["snaps"], key=parse_ts)
        return entry["snaps"][latest_ts]


class CyclingFixtureVenue(FixtureVenue):
    """Demo-mode venue: walks each market's recorded snapshots in order.

    Every ``get_book`` call advances that market's cursor to the next
    recorded timestamp (wrapping at the end), so a demo collection loop
    exercises the same code path as live collection while serving only
    fixture data. Snapshots keep their fixture timestamps and venue labels;
    callers are responsible for labeling stored rows as ``demo``.
    """

    def __init__(self, root: str | Path) -> None:
        super().__init__(root)
        self._cursors: dict[str, int] = {}

    def get_book(self, market_id: str) -> BookSnapshot:
        entry = self._entry(market_id)
        ordered = [entry["snaps"][ts] for ts in sorted(entry["snaps"], key=parse_ts)]
        cursor = self._cursors.get(market_id, 0)
        snap = ordered[cursor % len(ordered)]
        self._cursors[market_id] = cursor + 1
        return snap
