"""Tests for run modes, credentials, dry-run, digest separation, migration."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from pmwatch.credentials import CredentialError, check_kalshi_credentials
from pmwatch.modes import ModeError, load_config, resolve_mode
from pmwatch.store import SCHEMA_VERSION, Store

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures"
PAIRS = REPO_ROOT / "config" / "pairs.example.yaml"

KALSHI_ENV = {k: v for k, v in os.environ.items() if not k.startswith("KALSHI_")}


def run_cli(*argv: str, env: dict | None = None, cwd: Path = REPO_ROOT):
    return subprocess.run(
        [sys.executable, "-m", "pmwatch", *argv],
        capture_output=True, text=True, cwd=cwd, timeout=60, env=env,
    )


# ------------------------------------------------------------- mode resolution


def test_mode_defaults_to_fixture(monkeypatch):
    monkeypatch.delenv("PMWATCH_MODE", raising=False)
    assert resolve_mode(None, {}) == "fixture"


def test_mode_precedence_cli_over_env_over_yaml(monkeypatch):
    monkeypatch.setenv("PMWATCH_MODE", "demo")
    assert resolve_mode("live", {"mode": "fixture"}) == "live"
    assert resolve_mode(None, {"mode": "fixture"}) == "demo"
    monkeypatch.delenv("PMWATCH_MODE")
    assert resolve_mode(None, {"mode": "demo"}) == "demo"


def test_invalid_mode_names_remedy(monkeypatch):
    monkeypatch.delenv("PMWATCH_MODE", raising=False)
    with pytest.raises(ModeError, match="fixture needs no keys"):
        resolve_mode("production", {})


def test_config_file_unknown_key(tmp_path):
    cfg = tmp_path / "pmwatch.yaml"
    cfg.write_text("mode: demo\nbogus: 1\n")
    with pytest.raises(ModeError, match="unknown setting"):
        load_config(cfg)


# ----------------------------------------------------------------- credentials


def test_kalshi_credentials_missing_both():
    with pytest.raises(CredentialError, match="KALSHI_API_KEY") as exc:
        check_kalshi_credentials(env={})
    msg = str(exc.value)
    assert "KALSHI_API_SECRET" in msg
    assert "remedy" in msg
    assert "demo mode" in msg  # points at the keyless path


def test_kalshi_credentials_missing_secret():
    with pytest.raises(CredentialError, match="KALSHI_API_SECRET"):
        check_kalshi_credentials(env={"KALSHI_API_KEY": "abc123"})


def test_kalshi_credentials_bad_pem():
    pytest.importorskip("cryptography")
    with pytest.raises(CredentialError, match="does not parse as a PEM"):
        check_kalshi_credentials(
            env={"KALSHI_API_KEY": "abc123", "KALSHI_API_SECRET": "not a pem"}
        )


def test_kalshi_credentials_valid_pem():
    crypto = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    creds = check_kalshi_credentials(
        env={"KALSHI_API_KEY": "abc123", "KALSHI_API_SECRET": pem}
    )
    assert creds.api_key == "abc123"


# -------------------------------------------------------------------- dry-run


def test_live_dry_run_without_credentials_explains_what_is_missing(tmp_path):
    proc = run_cli(
        "live", "--pairs", str(PAIRS), "--interval", "300",
        "--db", str(tmp_path / "live.db"), "--out", str(tmp_path / "digest.md"),
        "--dry-run",
        env=KALSHI_ENV,
    )
    assert proc.returncode == 2
    assert "KALSHI_API_KEY" in proc.stderr
    assert "remedy" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert not (tmp_path / "live.db").exists()  # nothing written


def test_live_dry_run_with_credentials_validates_offline(tmp_path):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    env = {**KALSHI_ENV, "KALSHI_API_KEY": "abc123", "KALSHI_API_SECRET": pem}
    proc = run_cli(
        "live", "--pairs", str(PAIRS), "--interval", "300",
        "--db", str(tmp_path / "live.db"), "--out", str(tmp_path / "digest.md"),
        "--dry-run",
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "dry-run" in proc.stdout
    assert "fed-cut-sep-2026" in proc.stdout
    assert "no network calls" in proc.stdout
    assert "parses as a PEM private key" in proc.stdout
    assert not (tmp_path / "live.db").exists()  # dry-run writes nothing


# ----------------------------------------------------------- demo mode + digest


def test_demo_mode_end_to_end_and_digest_sections(tmp_path):
    db = tmp_path / "demo.db"
    digest = tmp_path / "digest.md"
    proc = run_cli(
        "demo", "--pairs", str(PAIRS), "--db", str(db),
        "--fixtures", str(FIXTURES), "--max-iterations", "2",
        "--out", str(digest),
        env=KALSHI_ENV,  # proves demo needs no keys
    )
    assert proc.returncode == 0, proc.stderr
    assert "DEMO mode" in proc.stdout

    with Store(db) as store:
        assert store.get_meta("source") == "demo"

    text = digest.read_text()
    assert "## Venue status" in text
    assert "## Matched-pair coverage" in text
    assert "## Demo section" in text
    assert "NOT live venue data" in text
    assert "trading advice" in text


def test_fixture_replay_digest_layout_unchanged(tmp_path):
    """A pure fixture replay renders the original single-section digest."""
    db = tmp_path / "replay.db"
    proc = run_cli("replay", "--fixtures", str(FIXTURES), "--db", str(db))
    assert proc.returncode == 0, proc.stderr
    report = run_cli("report", "--db", str(db), "--date", "2026-07-30")
    assert report.returncode == 0, report.stderr
    text = report.stdout
    assert "Generated from bundled fixtures" in text
    assert "## Demo section" not in text
    assert "## Live section" not in text
    assert "## Venue status" not in text


# ------------------------------------------------------------------ migration


def test_store_migrates_v1_database(tmp_path):
    """A v1 database (no fetched_at column) upgrades without losing rows."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, venue TEXT NOT NULL,
            market_id TEXT NOT NULL, question TEXT NOT NULL DEFAULT '',
            best_bid REAL, best_ask REAL, mid REAL, spread REAL,
            bid_depth_2c REAL, ask_depth_2c REAL, book_json TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'live',
            UNIQUE (ts, venue, market_id)
        );
        INSERT INTO snapshots (ts, venue, market_id, book_json)
        VALUES ('2026-07-30T14:00:00Z', 'kalshi', 'KXFEDCUT-26SEP', '{}');
        """
    )
    conn.commit()
    conn.close()

    with Store(db) as store:
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(snapshots)")}
        assert "fetched_at" in cols
        rows = store.snapshots_for_date("2026-07-30")
        assert len(rows) == 1
        assert rows[0]["market_id"] == "KXFEDCUT-26SEP"
        (version,) = store.conn.execute("PRAGMA user_version").fetchone()
        assert version == SCHEMA_VERSION
        # v3: a database created before `resolutions` existed still gains it,
        # because SCHEMA is executed on every open rather than only at create.
        tables = {
            r[0] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "resolutions" in tables


# ------------------------------------------------------------- net plumbing


def test_backoff_retries_5xx_then_succeeds():
    import httpx

    from pmwatch.net import get_json_with_backoff

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(500, json={"error": "x"})
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = get_json_with_backoff(
        client, "http://v/api", venue="test", sleep=lambda s: None
    )
    assert result == {"ok": True}
    assert len(calls) == 3


def test_backoff_does_not_retry_4xx():
    import httpx

    from pmwatch.net import get_json_with_backoff
    from pmwatch.venues.base import VenueError

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(404, json={"error": "nope"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(VenueError, match="test request failed"):
        get_json_with_backoff(client, "http://v/api", venue="test")
    assert len(calls) == 1


def test_backoff_gives_up_after_max_retries():
    import httpx

    from pmwatch.net import get_json_with_backoff
    from pmwatch.venues.base import VenueError

    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503, json={}))
    )
    with pytest.raises(VenueError):
        get_json_with_backoff(
            client, "http://v/api", venue="test", max_retries=2,
            sleep=lambda s: None,
        )


def test_rate_limiter_enforces_min_interval():
    import time

    from pmwatch.net import RateLimiter

    limiter = RateLimiter(min_interval_s=0.05)
    limiter.wait()
    start = time.monotonic()
    limiter.wait()
    assert time.monotonic() - start >= 0.049
