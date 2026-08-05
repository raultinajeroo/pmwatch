"""SQLite persistence for snapshots and dislocations.

Two tables plus a small meta table:

- ``snapshots``: one row per (ts, venue, market_id) with best-level stats and
  the full book as JSON. Upserted, so re-running a replay is idempotent.
- ``dislocations``: one row per episode, keyed on
  (pair, kind, direction, first_seen) so replays stay idempotent.
- ``meta``: provenance. ``source`` is ``"fixtures"``, ``"demo"``, or
  ``"live"`` and drives the honesty footer in reports.

Schema versioning uses ``PRAGMA user_version``: version 2 adds the
``fetched_at`` column (wall-clock fetch time; snapshot ``ts`` may be a
fixture timestamp) to existing databases via ``ALTER TABLE``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .detect import book_stats
from .models import BookSnapshot, Dislocation, format_ts

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    venue         TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    question      TEXT NOT NULL DEFAULT '',
    best_bid      REAL,
    best_ask      REAL,
    mid           REAL,
    spread        REAL,
    bid_depth_2c  REAL,
    ask_depth_2c  REAL,
    book_json     TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'live',
    fetched_at    TEXT,
    UNIQUE (ts, venue, market_id)
);
CREATE TABLE IF NOT EXISTS dislocations (
    id           INTEGER PRIMARY KEY,
    pair         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    direction    TEXT NOT NULL DEFAULT '',
    max_edge     REAL NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    count        INTEGER NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    source       TEXT NOT NULL DEFAULT 'live',
    UNIQUE (pair, kind, direction, first_seen)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_MIGRATIONS = {
    # version -> SQL applied to upgrade an existing database to that version
    2: ("ALTER TABLE snapshots ADD COLUMN fetched_at TEXT",),
}


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an existing database up to SCHEMA_VERSION, step by step."""
        (version,) = self.conn.execute("PRAGMA user_version").fetchone()
        for target in sorted(_MIGRATIONS):
            if version < target:
                for statement in _MIGRATIONS[target]:
                    try:
                        self.conn.execute(statement)
                    except sqlite3.OperationalError as exc:
                        # Column already added by a fresh CREATE TABLE.
                        if "duplicate column" not in str(exc):
                            raise
        if version < SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- meta ---------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # -- snapshots ----------------------------------------------------------

    def upsert_snapshot(
        self,
        snap: BookSnapshot,
        source: str = "live",
        fetched_at: str | None = None,
    ) -> None:
        stats = book_stats(snap)
        self.conn.execute(
            """
            INSERT INTO snapshots
                (ts, venue, market_id, question, best_bid, best_ask, mid,
                 spread, bid_depth_2c, ask_depth_2c, book_json, source,
                 fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts, venue, market_id) DO UPDATE SET
                question=excluded.question, best_bid=excluded.best_bid,
                best_ask=excluded.best_ask, mid=excluded.mid,
                spread=excluded.spread, bid_depth_2c=excluded.bid_depth_2c,
                ask_depth_2c=excluded.ask_depth_2c,
                book_json=excluded.book_json, source=excluded.source,
                fetched_at=excluded.fetched_at
            """,
            (
                format_ts(snap.ts), snap.venue, snap.market_id, snap.question,
                stats["best_bid"], stats["best_ask"], stats["mid"],
                stats["spread"], stats["bid_depth_2c"], stats["ask_depth_2c"],
                json.dumps(snap.to_dict()), source, fetched_at,
            ),
        )
        self.conn.commit()

    # -- dislocations -------------------------------------------------------

    def upsert_dislocation(self, d: Dislocation, source: str = "live") -> None:
        self.conn.execute(
            """
            INSERT INTO dislocations
                (pair, kind, direction, max_edge, first_seen, last_seen,
                 count, details_json, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pair, kind, direction, first_seen) DO UPDATE SET
                max_edge=excluded.max_edge, last_seen=excluded.last_seen,
                count=excluded.count, details_json=excluded.details_json,
                source=excluded.source
            """,
            (
                d.pair, d.kind, d.direction, d.edge,
                format_ts(d.detected_ts), format_ts(d.last_seen_ts),
                d.count, json.dumps(d.details), source,
            ),
        )
        self.conn.commit()

    # -- queries ------------------------------------------------------------

    def snapshots_for_date(self, date: str, source: str | None = None) -> list[sqlite3.Row]:
        if source is None:
            return self.conn.execute(
                "SELECT * FROM snapshots WHERE ts LIKE ? ORDER BY ts, venue",
                (f"{date}%",),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE ts LIKE ? AND source = ? "
            "ORDER BY ts, venue",
            (f"{date}%", source),
        ).fetchall()

    def dislocations_for_date(self, date: str, source: str | None = None) -> list[sqlite3.Row]:
        """Episodes whose first_seen falls on ``date`` (YYYY-MM-DD)."""
        # Arb episodes first (actionable in principle), then by max edge.
        if source is None:
            return self.conn.execute(
                "SELECT * FROM dislocations WHERE first_seen LIKE ? "
                "ORDER BY kind != 'arb', max_edge DESC",
                (f"{date}%",),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM dislocations WHERE first_seen LIKE ? AND source = ? "
            "ORDER BY kind != 'arb', max_edge DESC",
            (f"{date}%", source),
        ).fetchall()

    def sources_present(self, date: str) -> list[str]:
        """Distinct provenance labels stored for ``date``."""
        rows = self.conn.execute(
            "SELECT source FROM snapshots WHERE ts LIKE ? "
            "UNION SELECT source FROM dislocations WHERE first_seen LIKE ?",
            (f"{date}%", f"{date}%"),
        ).fetchall()
        return sorted(r["source"] for r in rows)

    def snapshot_counts_by_venue(self, date: str, source: str | None = None) -> list[sqlite3.Row]:
        if source is None:
            return self.conn.execute(
                "SELECT venue, market_id, COUNT(*) AS n, MIN(ts) AS first_ts, "
                "MAX(ts) AS last_ts FROM snapshots WHERE ts LIKE ? "
                "GROUP BY venue, market_id ORDER BY venue",
                (f"{date}%",),
            ).fetchall()
        return self.conn.execute(
            "SELECT venue, market_id, COUNT(*) AS n, MIN(ts) AS first_ts, "
            "MAX(ts) AS last_ts FROM snapshots WHERE ts LIKE ? AND source = ? "
            "GROUP BY venue, market_id ORDER BY venue",
            (f"{date}%", source),
        ).fetchall()

    def latest_snapshot_date(self) -> str | None:
        """UTC date (YYYY-MM-DD) of the most recent stored snapshot."""
        row = self.conn.execute("SELECT MAX(ts) AS m FROM snapshots").fetchone()
        return row["m"][:10] if row and row["m"] else None

    def timeline_for_market(self, date: str, venue: str, market_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT ts FROM snapshots WHERE ts LIKE ? AND venue = ? "
            "AND market_id = ? ORDER BY ts",
            (f"{date}%", venue, market_id),
        ).fetchall()
        return [r["ts"] for r in rows]
