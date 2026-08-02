"""Loading and validation of matched-pair configuration.

A pairs file is YAML with a top-level ``pairs`` list::

    pairs:
      - name: fed-cut-sep-2026
        venue_a_id: "polymarket:7142..."
        venue_b_id: "kalshi:KXFEDCUT-26SEP"
        fee_bps_a: 0        # Polymarket taker fee approximation
        fee_bps_b: 50       # Kalshi taker fee approximation
        question: "..."     # optional, informational

The same schema is used for the per-pair ``pair.yaml`` files that live next
to fixtures (single mapping instead of a ``pairs`` list).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import MatchedPair

KNOWN_VENUES = {"polymarket", "kalshi"}

_REQUIRED = ("name", "venue_a_id", "venue_b_id")


class PairConfigError(Exception):
    """Raised when a pairs file is structurally invalid."""


def _validate_pair(raw: dict, source: str) -> MatchedPair:
    if not isinstance(raw, dict):
        raise PairConfigError(f"{source}: each pair must be a mapping, got {type(raw)}")
    problems: list[str] = []
    for key in _REQUIRED:
        if not raw.get(key):
            problems.append(f"missing required field {key!r}")
    for key in ("fee_bps_a", "fee_bps_b"):
        value = raw.get(key, 0.0)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            problems.append(f"{key!r} must be a non-negative number, got {value!r}")
    for key in ("venue_a_id", "venue_b_id"):
        value = raw.get(key)
        if not value:
            continue
        try:
            venue, _ = MatchedPair.split_id(str(value))
        except ValueError as exc:
            problems.append(f"{key!r}: {exc}")
            continue
        if venue not in KNOWN_VENUES:
            problems.append(
                f"{key!r}: unknown venue {venue!r} (known: {sorted(KNOWN_VENUES)})"
            )
    if raw.get("venue_a_id") and raw.get("venue_a_id") == raw.get("venue_b_id"):
        problems.append("venue_a_id and venue_b_id are identical")
    if problems:
        joined = "; ".join(problems)
        raise PairConfigError(f"{source}: invalid pair {raw.get('name')!r}: {joined}")
    return MatchedPair(
        name=str(raw["name"]),
        venue_a_id=str(raw["venue_a_id"]),
        venue_b_id=str(raw["venue_b_id"]),
        fee_bps_a=float(raw.get("fee_bps_a", 0.0)),
        fee_bps_b=float(raw.get("fee_bps_b", 0.0)),
        question=str(raw.get("question", "")),
    )


def load_pairs(path: str | Path) -> list[MatchedPair]:
    """Load matched pairs from a YAML file.

    Accepts either ``{"pairs": [...]}`` or a single pair mapping (the
    ``pair.yaml`` fixture-metadata format).
    """
    path = Path(path)
    if not path.is_file():
        raise PairConfigError(f"pairs file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PairConfigError(f"{path}: YAML parse error: {exc}") from exc
    if data is None:
        raise PairConfigError(f"{path}: empty file")
    if isinstance(data, dict) and "pairs" in data:
        raws = data["pairs"]
        if not isinstance(raws, list) or not raws:
            raise PairConfigError(f"{path}: 'pairs' must be a non-empty list")
    elif isinstance(data, dict):
        raws = [data]
    else:
        raise PairConfigError(f"{path}: expected a mapping at the top level")
    return [_validate_pair(raw, str(path)) for raw in raws]
