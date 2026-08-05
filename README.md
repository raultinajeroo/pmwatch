# pmwatch

Watching prediction-market venues disagree with each other.

pmwatch is a cross-venue prediction-market microstructure monitor. It
snapshots order books for matched markets across venues (Polymarket primary,
Kalshi via adapter), detects cross-venue price dislocations (including the
buy-YES-on-A + buy-NO-on-B arbitrage, net of configurable fees), stores
everything in SQLite, and writes a daily markdown research digest.

## Why

Prediction markets have grown from a novelty into real, persistent venues
for event risk, and the same event now trades on multiple venues at once.
But cross-venue microstructure (where the books disagree, by how much, and
for how long) is under-instrumented relative to its interest: most tooling
watches one venue at a time. pmwatch is a small, honest instrument for
studying that gap.

It is a **research/learning instrument**. It is strictly **read-only**
(there is no order-placement code anywhere in this repository) and nothing
it produces is trading advice.

## Architecture

```
  Polymarket (gamma + CLOB APIs) ──┐
                                   ├─> VenueClient adapters ─> BookSnapshot
  Kalshi (v2 API, RSA-signed)   ──┘        (YES-terms book,    (best bid/ask,
                                            NO ask derived)     mid, spread,
                                                                depth @2c)
                                                │
                                                v
                                      DislocationEngine
                                      (arb edge math, hysteresis,
                                       persistence, divergence)
                                                │
                          ┌─────────────────────┼─────────────────────┐
                          v                     v                     v
                     SQLite store          replay mode           daily digest
                  (snapshots +          (offline, fixture-      (markdown:
                   dislocations,         driven, same code      dislocations,
                   provenance meta)      path as live)          book stats,
                                                                data quality)
```

All prices are dollars per YES-token unit. The NO best ask is derived from
the YES best bid (`no_ask = 1 - best_yes_bid`), which is exact for binary
markets and lets both venues share units.

## Quickstart (offline, no API keys, no network)

The repository ships with fixture snapshots for two matched pairs
(12 timesteps each on 2026-07-30). One pair plants a persistent 2.3c
cross-venue arb for 6 snapshots and then closes; the other stays aligned.
Everything below runs offline and every number is fixture-derived.

```bash
pip install -e .
pmwatch replay --fixtures fixtures/ --db /tmp/pmwatch_replay.db
```

```
pmwatch replay — fixtures: fixtures/
database: /tmp/pmwatch_replay.db
processed 24 timesteps, 48 snapshots, 2 matched pairs

pair                   kind         max_edge  snaps  first_seen           last_seen           
----------------------------------------------------------------------------------------------
fed-cut-sep-2026       arb            0.0230      6  2026-07-30T14:00:00Z 2026-07-30T14:25:00Z
fed-cut-sep-2026       divergence     0.0310      6  2026-07-30T14:00:00Z 2026-07-30T14:25:00Z
  us-recession-2026: no dislocations detected

note: fixture-derived results, not live market observations
```

```bash
pmwatch report --db /tmp/pmwatch_replay.db --date 2026-07-30
```

```markdown
# pmwatch daily digest — 2026-07-30

48 snapshots across 4 markets; 1 arb episode(s), 1 divergence episode(s).

## Top dislocations

| pair | kind | max edge ($) | snapshots | first seen (UTC) | last seen (UTC) |
|---|---|---:|---:|---|---|
| fed-cut-sep-2026 | arb | +0.0230 | 6 | 2026-07-30T14:00:00Z | 2026-07-30T14:25:00Z |
| fed-cut-sep-2026 | divergence | +0.0310 | 6 | 2026-07-30T14:00:00Z | 2026-07-30T14:25:00Z |

- `fed-cut-sep-2026` (AB): buy YES `polymarket:7142...` @ 0.575 + buy NO `kalshi:KXFEDCUT-26SEP` @ 0.400, fees $0.0020/unit.

## Book statistics

| venue | market | question | snapshots | avg mid | avg spread ($) | min mid | max mid |
|---|---|---|---:|---:|---:|---:|---:|
| kalshi | KXFEDCUT-26SEP | Fed rate cut at the September 2026 FOMC meeting? | 12 | 0.601 | 0.005 | 0.599 | 0.603 |
| kalshi | KXRECESSION-26 | US recession officially declared in 2026? | 12 | 0.218 | 0.006 | 0.218 | 0.218 |
| polymarket | 10991116138533105... | US recession officially declared in 2026? | 12 | 0.221 | 0.006 | 0.221 | 0.221 |
| polymarket | 71421073552824748... | Fed rate cut at the September 2026 FOMC meeting? | 12 | 0.579 | 0.006 | 0.571 | 0.587 |

## Data quality

| venue | market | snapshots | first | last | gaps |
|---|---|---:|---|---|---|
| kalshi | KXFEDCUT-26SEP | 12 | 2026-07-30T14:00:00Z | 2026-07-30T14:55:00Z | none |
...

---
Generated from bundled fixtures (`fixtures/`) via offline replay — these are handcrafted, fixture-derived numbers, not observations of live markets.

_pmwatch is a read-only research instrument. Nothing in this report is trading advice._
```

(Digest truncated for the README; run it to see the full data-quality table.)

## Detection math

For a matched binary pair (venue A, venue B), with best-ask YES price `aA`
on A and best-ask NO price `nB` on B (derived as `1 - best YES bid` on B):

```
edge_AB = 1 - aA - nB - fees
fees    = (aA * fee_bps_a + nB * fee_bps_b) / 10_000
```

`edge_BA` is the symmetric direction (YES on B + NO on A). A positive edge
means the two legs cost less than $1 while paying exactly $1 in every state
of the world.

Episodes, not single prints:

- **Open**: edge >= `min_edge` (default 0.01, i.e. 1c) for at least
  `min_persistence` consecutive snapshots (default 3).
- **Hysteresis**: once open, an episode stays open until edge < 0.5c
  (`close_edge`), so a marginal signal doesn't flap.
- **Divergence**: `|mid_A - mid_B| >= divergence_threshold` (default 0.03)
  is tracked separately as `kind="divergence"`, which is informational: two
  venues can disagree on mid without any tradeable cross, since each book has
  its own spread.

A `Dislocation` record aggregates an episode: first/last seen, observation
count, max edge, and a details dict with the price legs (so every flagged
edge can be audited against the stored books). All thresholds are CLI flags.

## Modes: fixture, demo, live

pmwatch has exactly three ways to obtain snapshots, and every stored row
and digest section is labeled with the one that produced it.

- **fixture** (default): the offline replay above. No keys, no network.
- **demo**: the *live collection code path* driven entirely from fixture
  data — pairing, engine, storage, and digest all exercised, no keys, no
  network. Stored rows are labeled `demo`, and the digest gets its own
  clearly-marked Demo section with venue status and matched-pair coverage.

  ```bash
  pmwatch demo --pairs config/pairs.example.yaml --db /tmp/pmwatch_demo.db \
               --max-iterations 3 --out digest_demo.md
  ```

- **live**: real venue API calls. Live mode refuses to start without the
  required credentials and never falls back silently to fixture data.

  ```bash
  # validate pair matching and credentials first: no network, no writes
  pmwatch live --pairs config/pairs.example.yaml --interval 300 \
               --db pmwatch_live.db --out digest_live.md --dry-run

  # then collect for real (Ctrl-C to stop; digest written at exit)
  pmwatch live --pairs config/pairs.example.yaml --interval 300 \
               --db pmwatch_live.db --out digest_live.md
  ```

The mode can also come from `PMWATCH_MODE` or a yaml config (`--config`,
`$PMWATCH_CONFIG`, or `./pmwatch.yaml` with keys `mode`, `db`, `interval`,
`fixtures`, `out`); a `--mode` flag wins over both. The legacy `collect`
and `watch` commands take `--mode demo|live` and default to fixture, where
they point you at `replay` instead of guessing.

### Credentials (live mode only)

- **Polymarket** read endpoints need no credentials. Market ids are CLOB
  token ids; find them via
  `curl 'https://gamma-api.polymarket.com/markets?slug=<slug>'`
  (field `clobTokenIds`, first entry is the YES token). Docs:
  <https://docs.polymarket.com>
- **Kalshi** signs read requests with RSA-PSS and needs two environment
  variables: `KALSHI_API_KEY` and `KALSHI_API_SECRET` (the PEM private
  key), plus `pip install ".[live]"` for the `cryptography` package. If
  anything is missing or the PEM does not parse, live mode exits with a
  message naming exactly what is missing and how to provide it — before
  any network call, never a traceback. `--dry-run` checks all of this
  offline.

Live requests go through a shared rate limiter (minimum interval per
venue) with bounded retries and jittered exponential backoff. Every stored
snapshot records its provenance (`source` column: fixture/demo/live) and
its wall-clock fetch time (`fetched_at`, added by the v2 schema migration;
old databases upgrade automatically).

Matched pairs are configured in a YAML file (see
`config/pairs.example.yaml` for the annotated format). Matching markets
across venues is manual work: resolution sources, deadlines, and settlement
rules must genuinely agree before two markets are comparable.

## Honest limits

- **The demo in this repository is fixture-based.** The bundled fixtures
  are handcrafted to realistic shapes; the 2.3c arb above is planted to
  exercise the detector, and every fixture-derived artifact says so in its
  footer. No live results are claimed anywhere in this repo.
- **No execution leg.** Detection assumes you could lift the full displayed
  size at best prices; it ignores queue position, partial fills, latency,
  and venue-specific settlement risk. A positive edge on screen is not a
  realized profit.
- **Fees are flat-bps configuration**, not venue truth. Kalshi's actual fee
  schedule is price-dependent; `fee_bps` is a conservative approximation you
  are expected to tune.
- **Matched pairs are manual** and small in number; there is no automated
  market matching.
- The engine's episode state is in-memory; `watch` rebuilds it on restart
  (stored episodes are persisted, open ones are not).

## Development

```bash
pip install -e ".[test]"
python -m pytest -q
```

The test suite is fully offline: venue adapters are exercised against the
bundled fixtures and synthetic books. CI runs the suite on Python 3.11 and
3.12.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Raul Tinajero Olivas.

pmwatch is a read-only research instrument. Nothing in this repository is
trading advice.
