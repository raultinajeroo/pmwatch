"""End-to-end CLI tests (offline) and pairs-config validation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pmwatch.match import PairConfigError, load_pairs

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures"


def run_cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pmwatch", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )


def test_replay_cli_end_to_end(tmp_path):
    db = tmp_path / "replay.db"
    proc = run_cli("replay", "--fixtures", str(FIXTURES), "--db", str(db))
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "fed-cut-sep-2026" in out
    assert "us-recession-2026" in out
    assert "arb" in out
    assert "0.0230" in out  # planted 2.3c edge, printed to 4 decimals
    assert "fixture-derived" in out
    # Replay is idempotent on the same db (upserts, not duplicate rows).
    proc2 = run_cli("replay", "--fixtures", str(FIXTURES), "--db", str(db))
    assert proc2.returncode == 0, proc2.stderr

    report = run_cli("report", "--db", str(db), "--date", "2026-07-30")
    assert report.returncode == 0, report.stderr
    assert "# pmwatch daily digest — 2026-07-30" in report.stdout
    assert "Generated from bundled fixtures" in report.stdout


def test_report_cli_missing_db(tmp_path):
    proc = run_cli("report", "--db", str(tmp_path / "nope.db"), "--date", "2026-07-30")
    assert proc.returncode == 2
    assert "database not found" in proc.stderr


def test_collect_cli_network_error_is_clean(tmp_path):
    # The sandbox blocks venue APIs; collect must fail cleanly (exit 2 with a
    # clear message), never with a traceback.
    pairs = REPO_ROOT / "config" / "pairs.example.yaml"
    proc = run_cli(
        "collect", "--pairs", str(pairs), "--db", str(tmp_path / "live.db"),
        "--fixtures", str(FIXTURES),
    )
    assert proc.returncode == 2
    assert "live collection failed" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_example_pairs_config_valid():
    pairs = load_pairs(REPO_ROOT / "config" / "pairs.example.yaml")
    assert len(pairs) == 2
    assert pairs[0].venue_a == "polymarket"
    assert pairs[0].venue_b == "kalshi"
    assert pairs[0].fee_bps_b == 50.0


def test_pairs_config_validation(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "pairs:\n"
        "  - name: broken\n"
        "    venue_a_id: 'polymarket:123'\n"
        "    venue_b_id: 'not-a-qualified-id'\n"
    )
    with pytest.raises(PairConfigError, match="venue:market_id"):
        load_pairs(bad)

    bad.write_text(
        "pairs:\n"
        "  - name: broken\n"
        "    venue_a_id: 'nyse:XYZ'\n"
        "    venue_b_id: 'kalshi:ABC'\n"
    )
    with pytest.raises(PairConfigError, match="unknown venue"):
        load_pairs(bad)

    bad.write_text("pairs: []\n")
    with pytest.raises(PairConfigError, match="non-empty"):
        load_pairs(bad)
