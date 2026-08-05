"""Run modes: fixture, demo, live.

pmwatch has exactly three ways to obtain snapshots, and every artifact is
labeled with the one that produced it:

- **fixture** — offline replay of the bundled fixtures (``pmwatch replay``).
  The default: no API keys, no network.
- **demo** — the live collection code path driven entirely from fixture
  data (``pmwatch demo``). Exercises pairing, the engine, storage, and the
  digest without keys or network; stored rows are labeled ``demo``.
- **live** — real venue API calls (``pmwatch live``). Refuses to start
  without the required credentials and never falls back silently.

Resolution order for the mode: ``--mode`` CLI flag, then the
``PMWATCH_MODE`` environment variable, then ``mode:`` in the YAML config
(``--config``, ``PMWATCH_CONFIG``, or ``./pmwatch.yaml``), else fixture.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

MODES = ("fixture", "demo", "live")
DEFAULT_MODE = "fixture"

ENV_MODE = "PMWATCH_MODE"
ENV_CONFIG = "PMWATCH_CONFIG"

#: Recognized keys in the optional YAML config file.
CONFIG_KEYS = ("mode", "db", "interval", "fixtures", "out")


class ModeError(ValueError):
    """Raised for an invalid mode or config, with a remedy in the message."""


def load_config(path: str | Path | None = None) -> dict:
    """Load the optional YAML config (explicit path, env, or ./pmwatch.yaml)."""
    candidate = path or os.environ.get(ENV_CONFIG)
    if candidate:
        cfg_path = Path(candidate)
        if not cfg_path.is_file():
            raise ModeError(f"config file not found: {cfg_path}")
    else:
        cfg_path = Path("pmwatch.yaml")
        if not cfg_path.is_file():
            return {}
    try:
        data = yaml.safe_load(cfg_path.read_text())
    except yaml.YAMLError as exc:
        raise ModeError(f"{cfg_path}: YAML parse error: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ModeError(f"{cfg_path}: expected a mapping of settings")
    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise ModeError(
            f"{cfg_path}: unknown setting(s) {unknown}; "
            f"recognized: {sorted(CONFIG_KEYS)}"
        )
    return data


def resolve_mode(cli_mode: str | None = None, config: dict | None = None) -> str:
    """Resolve the effective mode: CLI > env > yaml config > fixture."""
    source = "default"
    mode = cli_mode
    if mode is None:
        mode = os.environ.get(ENV_MODE)
        if mode:
            source = f"${ENV_MODE}"
    if mode is None and config:
        mode = config.get("mode")
        if mode:
            source = "config file"
    if mode is None:
        mode = DEFAULT_MODE
    mode = str(mode).strip().lower()
    if mode not in MODES:
        raise ModeError(
            f"invalid mode {mode!r} (from {source}); "
            f"choose one of: {', '.join(MODES)}. fixture needs no keys or "
            "network; demo runs the collection path on fixture data; live "
            "requires venue credentials"
        )
    return mode
