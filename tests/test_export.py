"""Exports must preserve the observation clock and exclude synthetic books."""

import csv
import json
from datetime import timedelta

import pytest

from pmwatch.cli import main
from pmwatch.models import BookSide, BookSnapshot, format_ts, parse_ts
from pmwatch.store import Store


START = parse_ts("2026-01-01T00:00:00Z")


def snapshot(store, minute, *, market="example", source="live", fetched=True,
             bid=0.3, ask=0.5, book_minute=None):
    snap = BookSnapshot(
        venue="kalshi", market_id=market, question="Synthetic export example?",
        ts=START + timedelta(minutes=minute if book_minute is None else book_minute),
        bids=[BookSide(bid, 10)], asks=[BookSide(ask, 10)],
    )
    store.upsert_snapshot(
        snap, source=source,
        fetched_at=format_ts(START + timedelta(minutes=minute)) if fetched else None,
    )


def resolve(store, market="example", *, outcome=1, resolved_ts=None):
    store.upsert_resolution(
        "kalshi", market, outcome=outcome,
        resolved_ts=resolved_ts or format_ts(START + timedelta(hours=2)),
        label_source="synthetic.test_label", venue_status="finalized",
        recorded_at=format_ts(START + timedelta(hours=3)),
    )


def test_longshot_export_filters_and_preserves_provenance(tmp_path):
    db, out = tmp_path / "books.db", tmp_path / "resolved.jsonl"
    with Store(db) as store:
        snapshot(store, 0, book_minute=-60)
        snapshot(store, 30, book_minute=-30)
        snapshot(store, 30, book_minute=-20, bid=0.2, ask=0.6)  # later row, same mid
        snapshot(store, 50, source="demo")
        snapshot(store, 55, source="fixtures")
        snapshot(store, 60, fetched=False)
        snapshot(store, 70, bid=0.8, ask=0.2)  # crossed book
        snapshot(store, 80, bid=-0.1, ask=0.3)
        snapshot(store, 120)  # at settlement
        snapshot(store, 130)  # after settlement
        snapshot(store, 0, market="pending")
        resolve(store)
        resolve(store, "pending", outcome=None)
        resolve(store, "empty")
        snapshot(store, 0, market="nonbinary")
        resolve(store, "nonbinary", outcome=2)
    before = db.read_bytes()
    assert main(["export", "--format", "longshot", "--db", str(db),
                 "--venue", "kalshi", "--out", str(out)]) == 0
    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["series"] == [[int(START.timestamp()), 0.4, 0.3, 0.5],
                                [int(START.timestamp()) + 1800, 0.4, 0.2, 0.6]]
    assert record["created_ts"] == int(START.timestamp())
    assert record["resolved_ts"] == int(START.timestamp()) + 7200
    assert record["outcome"] == 1
    assert record["provenance"]["price_estimator"] == "order_book_mid"
    assert record["provenance"]["timestamp_source"] == "fetched_at"
    assert record["provenance"]["created_ts_source"] == "first_observed"
    assert record["provenance"]["label_source"] == "synthetic.test_label"
    assert str(tmp_path) not in out.read_text()
    assert db.read_bytes() == before


def test_blameshift_export_is_one_market_sorted_unique_and_live(tmp_path):
    db, out = tmp_path / "books.db", tmp_path / "series.csv"
    with Store(db) as store:
        snapshot(store, 10, book_minute=-20)
        snapshot(store, 0, book_minute=-30)
        snapshot(store, 10, book_minute=-10, bid=0.5, ask=0.7)
        snapshot(store, 20, market="other")
        snapshot(store, 30, source="demo")
        snapshot(store, 40, fetched=False)
    assert main(["export", "--format", "blameshift", "--db", str(db),
                 "--venue", "kalshi", "--market-id", "example",
                 "--out", str(out)]) == 0
    with out.open() as fh:
        rows = list(csv.DictReader(fh))
    assert rows == [{"timestamp": "2026-01-01T00:00:00Z", "value": "0.4"},
                    {"timestamp": "2026-01-01T00:10:00Z", "value": "0.6"}]


@pytest.mark.parametrize("format", ["longshot", "blameshift"])
def test_export_empty_keeps_existing_output(tmp_path, capsys, format):
    db, out = tmp_path / "books.db", tmp_path / "output"
    with Store(db):
        pass
    out.write_text("keep this")
    args = ["export", "--format", format, "--db", str(db), "--venue", "kalshi",
            "--out", str(out)]
    if format == "blameshift":
        args += ["--market-id", "example"]
    assert main(args) == 2
    assert "no usable" in capsys.readouterr().err
    assert out.read_text() == "keep this"


def test_export_missing_database_is_not_created(tmp_path, capsys):
    db = tmp_path / "missing.db"
    assert main(["export", "--format", "longshot", "--db", str(db),
                 "--venue", "kalshi", "--out", str(tmp_path / "out")]) == 2
    assert "database not found" in capsys.readouterr().err
    assert not db.exists()


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_export_cannot_overwrite_database(tmp_path, alias):
    db = tmp_path / "books.db"
    with Store(db) as store:
        snapshot(store, 0)
        resolve(store)
    out = db if alias == "same" else tmp_path / "alias"
    if alias == "symlink":
        out.symlink_to(db)
    elif alias == "hardlink":
        out.hardlink_to(db)
    before = db.read_bytes()
    assert main(["export", "--format", "longshot", "--db", str(db),
                 "--venue", "kalshi", "--out", str(out)]) == 2
    assert db.read_bytes() == before


def test_blameshift_requires_a_single_market(tmp_path, capsys):
    assert main(["export", "--format", "blameshift", "--db", "unused.db",
                 "--venue", "kalshi", "--out", str(tmp_path / "out")]) == 2
    assert "--market-id" in capsys.readouterr().err


def test_collection_timestamps_each_response_after_it_arrives(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pmwatch.cli import _collect_once
    from pmwatch.models import MatchedPair

    clock = [START]

    class SlowClient:
        def get_book(self, market_id):
            clock[0] += timedelta(seconds=5)
            return BookSnapshot("kalshi", market_id, "Synthetic clock test", clock[0],
                                [BookSide(0.3, 10)], [BookSide(0.5, 10)])

    monkeypatch.setattr("pmwatch.cli.datetime", SimpleNamespace(now=lambda **kw: clock[0]))
    pair = MatchedPair(name="example", venue_a_id="kalshi:a", venue_b_id="kalshi:b")
    with Store(tmp_path / "books.db") as store:
        _collect_once([pair], {"kalshi": SlowClient()}, store,
                      SimpleNamespace(process=lambda *args: []), source="live")
        rows = store.conn.execute("SELECT market_id, fetched_at FROM snapshots ORDER BY market_id").fetchall()
    assert [(r["market_id"], r["fetched_at"]) for r in rows] == [
        ("a", "2026-01-01T00:00:05Z"), ("b", "2026-01-01T00:00:10Z"),
    ]
