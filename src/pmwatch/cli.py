"""pmwatch command line interface.

Subcommands:
    replay   run detection over fixture snapshots, offline
    report   write a daily markdown digest from a snapshot database
    collect  fetch one round of live books and run detection
    watch    collect in a loop at a fixed interval

Exit codes: 0 success; 2 user-facing error (config, network, fixtures).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .detect import DislocationEngine, book_stats
from .match import PairConfigError, load_pairs
from .models import Dislocation, MatchedPair, format_ts
from .replay import run_replay
from .report import generate_report
from .store import Store
from .venues.base import VenueClient, VenueError
from .venues.fixture import FixtureError

PROG = "pmwatch"


def _err(msg: str) -> int:
    print(f"{PROG}: error: {msg}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------------- replay


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        result = run_replay(
            args.fixtures,
            args.db,
            min_edge=args.min_edge,
            close_edge=args.close_edge,
            min_persistence=args.min_persistence,
            divergence_threshold=args.divergence_threshold,
        )
    except (FixtureError, PairConfigError) as exc:
        return _err(str(exc))

    print(f"pmwatch replay — fixtures: {args.fixtures}")
    print(f"database: {args.db}")
    print(
        f"processed {result.timesteps} timesteps, "
        f"{result.snapshots_stored} snapshots, {len(result.pairs)} matched pairs"
    )
    if result.gaps:
        for gap in result.gaps:
            print(f"  gap: {gap}")
    print()
    if not result.dislocations:
        print("no dislocations detected")
    else:
        print(
            f"{'pair':<22} {'kind':<11} {'max_edge':>9} {'snaps':>6}  "
            f"{'first_seen':<20} {'last_seen':<20}"
        )
        print("-" * 94)
        for d in sorted(result.dislocations, key=lambda d: (d.kind != "arb", -d.edge)):
            print(
                f"{d.pair:<22} {d.kind:<11} {d.edge:>9.4f} {d.count:>6}  "
                f"{format_ts(d.detected_ts):<20} {format_ts(d.last_seen_ts):<20}"
            )
    quiet = [p.name for p in result.pairs
             if all(d.pair != p.name for d in result.dislocations)]
    for name in quiet:
        print(f"  {name}: no dislocations detected")
    print()
    print("note: fixture-derived results, not live market observations")
    return 0


# --------------------------------------------------------------------------- report


def cmd_report(args: argparse.Namespace) -> int:
    if not Path(args.db).is_file():
        return _err(f"database not found: {args.db} (run replay or collect first)")
    with Store(args.db) as store:
        text = generate_report(store, args.date)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text, end="")
    return 0


# --------------------------------------------------------------------------- collect / watch


def _build_clients(args: argparse.Namespace) -> dict[str, VenueClient]:
    """Construct one client per venue named in the pairs file."""
    clients: dict[str, VenueClient] = {}
    for venue in args._venues_needed:
        if venue == "polymarket":
            from .venues.polymarket import PolymarketClient

            clients[venue] = PolymarketClient()
        elif venue == "kalshi":
            from .venues.kalshi import make_kalshi_client

            clients[venue] = make_kalshi_client(fixtures_dir=args.fixtures)
        else:
            raise PairConfigError(f"no client available for venue {venue!r}")
    return clients


def _collect_once(
    pairs: list[MatchedPair],
    clients: dict[str, VenueClient],
    store: Store,
    engine: DislocationEngine,
    source: str,
) -> list[Dislocation]:
    """One collection pass over all pairs. Returns newly closed episodes."""
    closed_all: list[Dislocation] = []
    for pair in pairs:
        snap_a = clients[pair.venue_a].get_book(pair.market_id_a)
        snap_b = clients[pair.venue_b].get_book(pair.market_id_b)
        store.upsert_snapshot(snap_a, source=source)
        store.upsert_snapshot(snap_b, source=source)
        closed = engine.process(pair, snap_a, snap_b)
        for d in closed:
            store.upsert_dislocation(d, source=source)
        closed_all.extend(closed)
        stats_a, stats_b = book_stats(snap_a), book_stats(snap_b)
        print(
            f"[{format_ts(datetime.now(tz=timezone.utc))}] {pair.name}: "
            f"{pair.venue_a} mid={stats_a['mid']:.3f}  "
            f"{pair.venue_b} mid={stats_b['mid']:.3f}",
            flush=True,
        )
        for d in closed:
            print(
                f"  closed {d.kind} episode: max_edge={d.edge:+.4f} "
                f"over {d.count} snapshots",
                flush=True,
            )
    return closed_all


def cmd_collect(args: argparse.Namespace) -> int:
    try:
        pairs = load_pairs(args.pairs)
        args._venues_needed = sorted({p.venue_a for p in pairs} | {p.venue_b for p in pairs})
        clients = _build_clients(args)
    except (PairConfigError, VenueError, FixtureError) as exc:
        return _err(str(exc))

    with Store(args.db) as store:
        store.set_meta("source", "live")
        engine = DislocationEngine(
            min_edge=args.min_edge,
            close_edge=args.close_edge,
            min_persistence=args.min_persistence,
            divergence_threshold=args.divergence_threshold,
        )
        try:
            _collect_once(pairs, clients, store, engine, source="live")
        except (VenueError, httpx.HTTPError) as exc:
            return _err(
                f"live collection failed (network or venue error): {exc}\n"
                "check connectivity and credentials; to explore offline, use "
                f"'{PROG} replay --fixtures fixtures/ --db {args.db}'"
            )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        pairs = load_pairs(args.pairs)
        args._venues_needed = sorted({p.venue_a for p in pairs} | {p.venue_b for p in pairs})
        clients = _build_clients(args)
    except (PairConfigError, VenueError, FixtureError) as exc:
        return _err(str(exc))

    with Store(args.db) as store:
        store.set_meta("source", "live")
        engine = DislocationEngine(
            min_edge=args.min_edge,
            close_edge=args.close_edge,
            min_persistence=args.min_persistence,
            divergence_threshold=args.divergence_threshold,
        )
        iteration = 0
        try:
            while True:
                iteration += 1
                try:
                    _collect_once(pairs, clients, store, engine, source="live")
                except (VenueError, httpx.HTTPError) as exc:
                    print(f"{PROG}: warning: collection pass failed: {exc}", file=sys.stderr)
                if args.max_iterations and iteration >= args.max_iterations:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\n{PROG}: stopped after {iteration} iteration(s)")
        for d in engine.finalize():
            store.upsert_dislocation(d, source="live")
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Cross-venue prediction-market microstructure monitor "
        "(read-only, research use; not trading advice).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_detector_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--min-edge", type=float, default=0.01,
                       help="arb open threshold in dollars (default 0.01 = 1c)")
        p.add_argument("--close-edge", type=float, default=0.005,
                       help="arb close threshold in dollars (default 0.005 = 0.5c)")
        p.add_argument("--min-persistence", type=int, default=3,
                       help="consecutive snapshots required to open an episode")
        p.add_argument("--divergence-threshold", type=float, default=0.03,
                       help="mid-price divergence threshold in dollars")

    p_replay = sub.add_parser("replay", help="offline detection over fixture snapshots")
    p_replay.add_argument("--fixtures", default="fixtures", help="fixture root directory")
    p_replay.add_argument("--db", default="pmwatch_replay.db", help="output SQLite database")
    add_detector_args(p_replay)
    p_replay.set_defaults(func=cmd_replay)

    p_report = sub.add_parser("report", help="daily markdown digest from a database")
    p_report.add_argument("--db", required=True, help="SQLite database path")
    p_report.add_argument("--date", default=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
                          help="UTC date, YYYY-MM-DD (default: today)")
    p_report.add_argument("--out", help="write digest to a file instead of stdout")
    p_report.set_defaults(func=cmd_report)

    p_collect = sub.add_parser("collect", help="one pass of live collection")
    p_collect.add_argument("--pairs", required=True, help="matched pairs YAML")
    p_collect.add_argument("--db", default="pmwatch.db", help="SQLite database path")
    p_collect.add_argument("--fixtures", default="fixtures",
                           help="fixture root for Kalshi paper mode (no API key)")
    add_detector_args(p_collect)
    p_collect.set_defaults(func=cmd_collect)

    p_watch = sub.add_parser("watch", help="collect in a loop")
    p_watch.add_argument("--pairs", required=True, help="matched pairs YAML")
    p_watch.add_argument("--db", default="pmwatch.db", help="SQLite database path")
    p_watch.add_argument("--fixtures", default="fixtures",
                         help="fixture root for Kalshi paper mode (no API key)")
    p_watch.add_argument("--interval", type=float, default=60.0,
                         help="seconds between passes (default 60)")
    p_watch.add_argument("--max-iterations", type=int,
                         help="stop after N passes (default: run until Ctrl-C)")
    add_detector_args(p_watch)
    p_watch.set_defaults(func=cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
