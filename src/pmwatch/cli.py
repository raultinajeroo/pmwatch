"""pmwatch command line interface.

Subcommands:
    replay   fixture mode: run detection over fixture snapshots, offline
    demo     demo mode: the collection code path driven from fixture data
    live     live mode: real venue API calls (requires credentials)
    report   write a daily markdown digest from a snapshot database
    collect  one collection pass (legacy; use demo/live via --mode)
    watch    collect in a loop at a fixed interval (legacy; see --mode)

Modes: fixture (default, offline replay), demo (collection path on fixture
data, no keys), live (real API calls, refuses to start without credentials).
Stored rows and digests are always labeled with the mode that produced them.

Exit codes: 0 success; 2 user-facing error (config, credentials, network,
fixtures). No error path prints a raw traceback.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .credentials import CredentialError, check_live_credentials
from .detect import DislocationEngine, book_stats
from .match import PairConfigError, load_pairs
from .models import Dislocation, MatchedPair, format_ts
from .modes import ModeError, load_config, resolve_mode
from .replay import run_replay
from .report import generate_report
from .store import Store
from .venues.base import VenueClient, VenueError
from .venues.fixture import CyclingFixtureVenue, FixtureError

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


# --------------------------------------------------------------------------- collection core


def _venues_needed(pairs: list[MatchedPair]) -> list[str]:
    return sorted({p.venue_a for p in pairs} | {p.venue_b for p in pairs})


def _build_clients(
    mode: str, venues: list[str], fixtures_dir: str
) -> dict[str, VenueClient]:
    """Construct one client per venue for the given mode.

    demo: every venue is served from fixtures (cycling through recorded
    snapshots). live: real clients, after strict credential validation.
    """
    clients: dict[str, VenueClient] = {}
    if mode == "demo":
        shared = CyclingFixtureVenue(fixtures_dir)
        for venue in venues:
            clients[venue] = shared
        return clients

    creds = check_live_credentials(venues)
    for venue in venues:
        if venue == "polymarket":
            from .venues.polymarket import PolymarketClient

            clients[venue] = PolymarketClient()
        elif venue == "kalshi":
            from .venues.kalshi import KalshiClient

            kalshi = creds["kalshi"]
            clients[venue] = KalshiClient(
                api_key=kalshi.api_key, private_key_pem=kalshi.private_key_pem
            )
        else:
            raise PairConfigError(f"no client available for venue {venue!r}")
    return clients


def _venue_status(mode: str, venues: list[str]) -> dict[str, str]:
    if mode == "demo":
        return {v: "demo (fixture data, no network)" for v in venues}
    labels = {
        "kalshi": "live (RSA-PSS signed requests)",
        "polymarket": "live (unauthenticated read endpoints)",
    }
    return {v: labels.get(v, "live") for v in venues}


def _collect_once(
    pairs: list[MatchedPair],
    clients: dict[str, VenueClient],
    store: Store,
    engine: DislocationEngine,
    source: str,
    fail_fast: bool = False,
) -> list[Dislocation]:
    """One collection pass over all pairs. Returns newly closed episodes.

    A pair that fails (venue error, or a book with an empty side, which is a
    legitimate market state) is skipped for this pass so the remaining pairs
    still get observed. Unless ``fail_fast``, in which case it propagates.
    """
    closed_all: list[Dislocation] = []
    fetched_at = format_ts(datetime.now(tz=timezone.utc))
    for pair in pairs:
        try:
            snap_a = clients[pair.venue_a].get_book(pair.market_id_a)
            snap_b = clients[pair.venue_b].get_book(pair.market_id_b)
            store.upsert_snapshot(snap_a, source=source, fetched_at=fetched_at)
            store.upsert_snapshot(snap_b, source=source, fetched_at=fetched_at)
            closed = engine.process(pair, snap_a, snap_b)
        except (VenueError, httpx.HTTPError, ValueError) as exc:
            if fail_fast:
                raise
            print(
                f"{PROG}: warning: {pair.name}: skipped this pass: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
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


def _run_collection(args: argparse.Namespace, mode: str) -> int:
    """Shared loop for demo/live/collect/watch."""
    try:
        pairs = load_pairs(args.pairs)
        venues = _venues_needed(pairs)
        clients = _build_clients(mode, venues, args.fixtures)
    except (PairConfigError, VenueError, FixtureError) as exc:
        return _err(str(exc))

    venue_status = _venue_status(mode, venues)
    if mode == "demo":
        print(
            "pmwatch DEMO mode: all snapshots come from fixture files "
            f"({args.fixtures}/), no network calls, no credentials used.",
            flush=True,
        )
    for venue, status in venue_status.items():
        print(f"venue status: {venue}: {status}", flush=True)

    with Store(args.db) as store:
        store.set_meta("source", mode if mode != "fixture" else "fixtures")
        store.set_meta("venue_status", json.dumps(venue_status))
        store.set_meta(
            "pairs",
            json.dumps(
                [
                    {
                        "name": p.name,
                        "venue_a_id": p.venue_a_id,
                        "venue_b_id": p.venue_b_id,
                    }
                    for p in pairs
                ]
            ),
        )
        engine = DislocationEngine(
            min_edge=args.min_edge,
            close_edge=args.close_edge,
            min_persistence=args.min_persistence,
            divergence_threshold=args.divergence_threshold,
        )
        max_iterations = getattr(args, "max_iterations", None)
        interval = getattr(args, "interval", 0.0) or 0.0
        fail_fast = getattr(args, "_fail_fast", False)
        iteration = 0
        try:
            while True:
                iteration += 1
                try:
                    _collect_once(
                        pairs, clients, store, engine, source=mode,
                        fail_fast=fail_fast,
                    )
                except (VenueError, httpx.HTTPError) as exc:
                    if fail_fast:
                        return _err(
                            f"{mode} collection failed (network or venue "
                            f"error): {exc}\ncheck connectivity and "
                            f"credentials; to explore offline, use "
                            f"'{PROG} replay --fixtures fixtures/ --db {args.db}' "
                            f"or '{PROG} demo --pairs {args.pairs} --db {args.db}'"
                        )
                    print(
                        f"{PROG}: warning: collection pass failed: {exc}",
                        file=sys.stderr,
                    )
                if max_iterations is not None and iteration >= max_iterations:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\n{PROG}: stopped after {iteration} iteration(s)")
        for d in engine.finalize():
            store.upsert_dislocation(d, source=mode)

        out = getattr(args, "out", None)
        if out:
            # Digest the date the stored data actually covers (fixture and
            # demo snapshots keep their recorded timestamps, not today's).
            date = (
                store.latest_snapshot_date()
                or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            )
            Path(out).write_text(generate_report(store, date))
            print(f"wrote {out} ({mode}-mode digest for {date})")
    return 0


# --------------------------------------------------------------------------- demo / live


def cmd_demo(args: argparse.Namespace) -> int:
    return _run_collection(args, "demo")


def cmd_live(args: argparse.Namespace) -> int:
    try:
        pairs = load_pairs(args.pairs)
        venues = _venues_needed(pairs)
        creds = check_live_credentials(venues)
    except (PairConfigError, CredentialError) as exc:
        return _err(str(exc))

    if args.dry_run:
        # Validation only: pairs parsed, credentials present and parseable.
        # No network calls, no database writes, nothing labeled live.
        print("pmwatch live --dry-run: validation only, no network calls, "
              "nothing written")
        print(f"pairs file: {args.pairs} ({len(pairs)} matched pair(s))")
        for p in pairs:
            print(f"  {p.name}: {p.venue_a_id} <-> {p.venue_b_id}")
        for venue in venues:
            if creds[venue] is None:
                print(f"credentials: {venue}: none required (read endpoints)")
            else:
                print(f"credentials: {venue}: KALSHI_API_KEY set, "
                      "KALSHI_API_SECRET parses as a PEM private key")
        print(f"interval: {args.interval}s  db: {args.db}  "
              f"out: {args.out or '(stdout digest disabled)'}")
        print("dry-run OK: rerun without --dry-run to start live collection")
        return 0

    return _run_collection(args, "live")


def cmd_collect(args: argparse.Namespace) -> int:
    mode = _resolve_collection_mode(args)
    if mode is None:
        return 2
    if mode == "fixture":
        return _err(
            "collect does not run in fixture mode; use "
            f"'{PROG} replay --fixtures fixtures/ --db {args.db}' for the "
            "offline fixture replay, or --mode demo / --mode live"
        )
    args.max_iterations = 1  # collect is always a single pass
    args._fail_fast = True
    return _run_collection(args, mode)


def cmd_watch(args: argparse.Namespace) -> int:
    mode = _resolve_collection_mode(args)
    if mode is None:
        return 2
    if mode == "fixture":
        return _err(
            "watch does not run in fixture mode; use "
            f"'{PROG} replay --fixtures fixtures/ --db {args.db}' for the "
            "offline fixture replay, or --mode demo / --mode live"
        )
    return _run_collection(args, mode)


def _resolve_collection_mode(args: argparse.Namespace) -> str | None:
    try:
        config = load_config(args.config)
        mode = resolve_mode(args.mode, config)
    except ModeError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr)
        return None
    # Config file values fill in anything left at its CLI default.
    for key, default in (("db", "pmwatch.db"), ("interval", 60.0),
                         ("fixtures", "fixtures"), ("out", None)):
        if key in config and getattr(args, key, default) == default:
            setattr(args, key, config[key])
    return mode


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

    def add_mode_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mode", choices=["fixture", "demo", "live"],
                       help="fixture (default; offline replay), demo "
                       "(collection path on fixture data), or live (real API "
                       "calls, requires credentials). Also settable via "
                       "PMWATCH_MODE or a yaml config.")
        p.add_argument("--config",
                       help="yaml config file (mode/db/interval/fixtures/out); "
                       "default: $PMWATCH_CONFIG or ./pmwatch.yaml if present")

    p_replay = sub.add_parser("replay", help="fixture mode: offline detection over fixture snapshots")
    p_replay.add_argument("--fixtures", default="fixtures", help="fixture root directory")
    p_replay.add_argument("--db", default="pmwatch_replay.db", help="output SQLite database")
    add_detector_args(p_replay)
    p_replay.set_defaults(func=cmd_replay)

    p_demo = sub.add_parser("demo", help="demo mode: collection path on fixture data, no keys")
    p_demo.add_argument("--pairs", required=True, help="matched pairs YAML")
    p_demo.add_argument("--db", default="pmwatch_demo.db", help="SQLite database path")
    p_demo.add_argument("--fixtures", default="fixtures", help="fixture root directory")
    p_demo.add_argument("--interval", type=float, default=0.0,
                        help="seconds between passes (default 0)")
    p_demo.add_argument("--max-iterations", type=int, default=3,
                        help="collection passes (default 3)")
    p_demo.add_argument("--out", help="also write a demo-mode digest to this file")
    add_detector_args(p_demo)
    p_demo.set_defaults(func=cmd_demo)

    p_live = sub.add_parser("live", help="live mode: real venue API calls (requires credentials)")
    p_live.add_argument("--pairs", required=True, help="matched pairs YAML")
    p_live.add_argument("--db", default="pmwatch_live.db", help="SQLite database path")
    p_live.add_argument("--fixtures", default="fixtures", help=argparse.SUPPRESS)
    p_live.add_argument("--interval", type=float, default=300.0,
                        help="seconds between passes (default 300)")
    p_live.add_argument("--max-iterations", type=int,
                        help="stop after N passes (default: run until Ctrl-C)")
    p_live.add_argument("--out", help="write a live-mode digest to this file at exit")
    p_live.add_argument("--dry-run", action="store_true",
                        help="validate pair matching and credentials only: no "
                        "network calls, no writes, nothing labeled live")
    add_detector_args(p_live)
    p_live.set_defaults(func=cmd_live)

    p_report = sub.add_parser("report", help="daily markdown digest from a database")
    p_report.add_argument("--db", required=True, help="SQLite database path")
    p_report.add_argument("--date", default=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
                          help="UTC date, YYYY-MM-DD (default: today)")
    p_report.add_argument("--out", help="write digest to a file instead of stdout")
    p_report.set_defaults(func=cmd_report)

    p_collect = sub.add_parser("collect", help="one collection pass (--mode demo|live)")
    p_collect.add_argument("--pairs", required=True, help="matched pairs YAML")
    p_collect.add_argument("--db", default="pmwatch.db", help="SQLite database path")
    p_collect.add_argument("--fixtures", default="fixtures",
                           help="fixture root for demo mode")
    add_mode_args(p_collect)
    add_detector_args(p_collect)
    p_collect.set_defaults(func=cmd_collect)

    p_watch = sub.add_parser("watch", help="collect in a loop (--mode demo|live)")
    p_watch.add_argument("--pairs", required=True, help="matched pairs YAML")
    p_watch.add_argument("--db", default="pmwatch.db", help="SQLite database path")
    p_watch.add_argument("--fixtures", default="fixtures",
                         help="fixture root for demo mode")
    p_watch.add_argument("--interval", type=float, default=60.0,
                         help="seconds between passes (default 60)")
    p_watch.add_argument("--max-iterations", type=int,
                         help="stop after N passes (default: run until Ctrl-C)")
    add_mode_args(p_watch)
    add_detector_args(p_watch)
    p_watch.set_defaults(func=cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
