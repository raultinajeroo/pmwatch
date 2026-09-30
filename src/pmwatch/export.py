"""Read-only exports of live order-book midpoints for downstream research."""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .models import format_ts, parse_ts


def _observations(conn, venue, market_id, *, before=None):
    rows = conn.execute(
        "SELECT fetched_at, mid FROM snapshots "
        "WHERE venue = ? AND market_id = ? AND source = 'live' "
        "AND fetched_at IS NOT NULL AND mid BETWEEN 0 AND 1 "
        "AND best_bid BETWEEN 0 AND 1 AND best_ask BETWEEN 0 AND 1 "
        "AND best_bid <= best_ask ORDER BY id",
        (venue, market_id),
    )
    # A second stored book in the same observation second wins. Normalize
    # offsets before sorting/deduplication; never substitute the book clock.
    points = {}
    for row in rows:
        observed = parse_ts(row["fetched_at"])
        if before is None or observed < before:
            points[observed] = row["mid"]
    return sorted(points.items())


def _longshot_records(conn, venue):
    resolutions = conn.execute(
        "SELECT * FROM resolutions WHERE venue = ? "
        "AND outcome IN (0, 1) AND resolved_ts IS NOT NULL ORDER BY market_id",
        (venue,),
    ).fetchall()
    records = []
    for resolution in resolutions:
        resolved = parse_ts(resolution["resolved_ts"])
        market_id = resolution["market_id"]
        points = _observations(conn, venue, market_id, before=resolved)
        if not points:
            continue
        question = conn.execute(
            "SELECT question FROM snapshots WHERE venue = ? AND market_id = ? "
            "AND source = 'live' ORDER BY id DESC LIMIT 1", (venue, market_id),
        ).fetchone()["question"]
        records.append({
            "venue": venue,
            "market_id": market_id,
            "question": question,
            "category": "uncategorized",
            # pmwatch does not store market creation; first observation is
            # a conservative lifetime bound, explicitly labeled as such.
            "created_ts": int(points[0][0].timestamp()),
            "resolved_ts": int(resolved.timestamp()),
            "outcome": resolution["outcome"],
            "volume": None,
            "n_traders": None,
            "series": [[int(ts.timestamp()), mid] for ts, mid in points],
            "provenance": {
                "source": "pmwatch",
                "snapshot_source": "live",
                "price_estimator": "order_book_mid",
                "timestamp_source": "fetched_at",
                "created_ts_source": "first_observed",
                "label_source": resolution["label_source"],
                "venue_status": resolution["venue_status"],
                "resolution_recorded_at": resolution["recorded_at"],
            },
        })
    return records


def export_data(db, out, *, format, venue, market_id=None) -> int:
    """Write one venue's resolved JSONL or one market's CSV; return row count.

    The database is opened read-only and held at one consistent snapshot.
    No schema migration, venue lookup, or collection is performed.
    """
    if format == "blameshift" and not market_id:
        raise ValueError("blameshift export requires --market-id (one series per file)")
    if format == "longshot" and market_id:
        raise ValueError("--market-id is only used with --format blameshift")
    if format not in ("longshot", "blameshift"):
        raise ValueError(f"unknown export format: {format}")
    db, out = Path(db), Path(out)
    if not db.is_file():
        raise ValueError(f"database not found: {db}")
    if out.exists() and out.samefile(db):
        raise ValueError("export output must not overwrite the database")
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        if format == "longshot":
            rows = _longshot_records(conn, venue)
        else:
            rows = [(format_ts(ts), mid)
                    for ts, mid in _observations(conn, venue, market_id)]
    if not rows:
        raise ValueError(
            "no usable live observations for this export; longshot also needs "
            "binary outcomes and settlement times recorded by pmwatch resolve"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        if format == "longshot":
            for record in rows:
                fh.write(json.dumps(record, allow_nan=False) + "\n")
        else:
            writer = csv.writer(fh)
            writer.writerow(["timestamp", "value"])
            writer.writerows(rows)
    return len(rows)
