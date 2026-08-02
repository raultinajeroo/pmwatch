"""Daily markdown digest from the snapshot store.

The digest is deliberately plain: tables render in any markdown viewer and
the numbers carry provenance. If the database was produced by replaying
bundled fixtures, the footer says so in words; fixture-derived output is
never presented as live observation.
"""

from __future__ import annotations

import json
from collections import defaultdict

from .store import Store

DISCLAIMER = (
    "_pmwatch is a read-only research instrument. Nothing in this report is "
    "trading advice._"
)


def _fmt_edge(value: float) -> str:
    return f"{value:+.4f}"


def _fmt_price(value) -> str:
    return "-" if value is None else f"{value:.3f}"


def _dislocation_table(rows: list) -> str:
    lines = [
        "| pair | kind | max edge ($) | snapshots | first seen (UTC) | last seen (UTC) |",
        "|---|---|---:|---:|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['pair']} | {r['kind']} | {_fmt_edge(r['max_edge'])} "
            f"| {r['count']} | {r['first_seen']} | {r['last_seen']} |"
        )
    return "\n".join(lines)


def _book_stats_section(rows: list) -> str:
    """Aggregate per (venue, market) book statistics over the day."""
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        groups[(r["venue"], r["market_id"])].append(r)
    lines = [
        "| venue | market | question | snapshots | avg mid | avg spread ($) | min mid | max mid |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for (venue, market_id), grp in sorted(groups.items()):
        mids = [g["mid"] for g in grp if g["mid"] is not None]
        spreads = [g["spread"] for g in grp if g["spread"] is not None]
        question = (grp[0]["question"] or "")[:48]
        short_id = market_id if len(market_id) <= 20 else market_id[:17] + "..."
        avg_mid = sum(mids) / len(mids) if mids else float("nan")
        avg_spread = sum(spreads) / len(spreads) if spreads else float("nan")
        lines.append(
            f"| {venue} | {short_id} | {question} | {len(grp)} "
            f"| {_fmt_price(avg_mid)} | {_fmt_price(avg_spread)} "
            f"| {_fmt_price(min(mids) if mids else None)} "
            f"| {_fmt_price(max(mids) if mids else None)} |"
        )
    return "\n".join(lines)


def _data_quality_section(store: Store, date: str) -> str:
    counts = store.snapshot_counts_by_venue(date)
    lines: list[str] = []
    if not counts:
        lines.append("No snapshots stored for this date.")
        return "\n".join(lines)

    lines.append("| venue | market | snapshots | first | last | gaps |")
    lines.append("|---|---|---:|---|---|---|")
    for r in counts:
        timeline = store.timeline_for_market(date, r["venue"], r["market_id"])
        gaps = _find_gaps(timeline)
        short_id = r["market_id"] if len(r["market_id"]) <= 20 else r["market_id"][:17] + "..."
        lines.append(
            f"| {r['venue']} | {short_id} | {r['n']} "
            f"| {r['first_ts']} | {r['last_ts']} | {gaps} |"
        )
    return "\n".join(lines)


def _find_gaps(timeline: list[str]) -> str:
    """Count irregular intervals in a snapshot timeline.

    A gap is an interval longer than 1.5x the modal spacing. Returns a short
    human string; 'none' when spacing is regular.
    """
    from .models import parse_ts

    if len(timeline) < 3:
        return "n/a"
    dts = [parse_ts(t) for t in timeline]
    deltas = [(b - a).total_seconds() for a, b in zip(dts, dts[1:])]
    modal = min(deltas)  # regular cadence is the shortest observed spacing
    gaps = sum(1 for d in deltas if d > modal * 1.5)
    return "none" if gaps == 0 else f"{gaps} interval(s) > {modal:.0f}s"


def generate_report(store: Store, date: str) -> str:
    """Build the markdown digest for one UTC date (YYYY-MM-DD)."""
    source = store.get_meta("source") or "unknown"
    dislocations = store.dislocations_for_date(date)
    snapshots = store.snapshots_for_date(date)

    arbs = [r for r in dislocations if r["kind"] == "arb"]
    divergences = [r for r in dislocations if r["kind"] == "divergence"]

    parts: list[str] = []
    parts.append(f"# pmwatch daily digest — {date}")
    parts.append("")
    n_markets = len({(s["venue"], s["market_id"]) for s in snapshots})
    parts.append(
        f"{len(snapshots)} snapshots across {n_markets} markets; "
        f"{len(arbs)} arb episode(s), {len(divergences)} divergence episode(s)."
    )
    parts.append("")

    parts.append("## Top dislocations")
    parts.append("")
    if dislocations:
        parts.append(_dislocation_table(list(dislocations)))
        parts.append("")
        for r in arbs:
            details = json.loads(r["details_json"] or "{}")
            if "yes_ask" in details:
                parts.append(
                    f"- `{r['pair']}` ({r['direction']}): buy YES "
                    f"`{details.get('buy_yes')}` @ {details['yes_ask']:.3f} + "
                    f"buy NO `{details.get('buy_no')}` @ {details['no_ask']:.3f}, "
                    f"fees ${details.get('fees', 0.0):.4f}/unit."
                )
        if arbs:
            parts.append("")
    else:
        parts.append("No dislocations detected.")
        parts.append("")

    parts.append("## Book statistics")
    parts.append("")
    parts.append(_book_stats_section(snapshots) if snapshots else "No snapshots.")
    parts.append("")

    parts.append("## Data quality")
    parts.append("")
    parts.append(_data_quality_section(store, date))
    parts.append("")

    parts.append("---")
    if source == "fixtures":
        fixtures_dir = store.get_meta("fixtures_dir") or "bundled fixtures"
        parts.append(
            f"Generated from bundled fixtures (`{fixtures_dir}`) via offline "
            "replay — these are handcrafted, fixture-derived numbers, not "
            "observations of live markets."
        )
    elif source == "live":
        parts.append("Generated from live venue API snapshots collected locally.")
    else:
        parts.append("Data provenance unknown (meta table empty).")
    parts.append("")
    parts.append(DISCLAIMER)
    parts.append("")
    return "\n".join(parts)
