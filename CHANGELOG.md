# Changelog

All notable changes to this project are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Explicit fixture/demo/live modes. `pmwatch demo` runs the collection
  code path on fixture data (no keys, no network); `pmwatch live` makes
  real venue API calls and refuses to start without the required
  credentials, with an exact remedy message. Mode resolves from `--mode`,
  `$PMWATCH_MODE`, or a yaml config (`config/pmwatch.example.yaml`);
  default stays fixture.
- `pmwatch live --dry-run`: validates pair matching and credentials
  offline — no network calls, no writes, nothing labeled live.
- Kalshi RSA-PSS credential validation (`KALSHI_API_KEY` /
  `KALSHI_API_SECRET` PEM) before any network call; new `[live]` extra
  carries `cryptography`.
- Shared rate limiting (per-venue minimum interval) and bounded retries
  with full jitter (`src/pmwatch/net.py`), used by both live adapters.
- Digest sections separated by provenance mode (fixture/demo/live) with
  venue status, matched-pair coverage, and persistence windows; pure
  fixture-replay digests keep their original layout.
- Snapshot `fetched_at` column via a PRAGMA user_version migration (old
  databases upgrade in place).
- ROADMAP.md for planned directions; README now documents only what exists.
- docs/IMPACT.md stating which metrics are collected (CI status only) and
  which are not (no telemetry, no adoption numbers).
- GitHub issue templates for bug reports and feature requests.
- fixtures/README.md documenting the bundled sample data and how to run
  the offline replay.

### Changed

- README quickstart now starts from `git clone` and links the roadmap.
- The silent Kalshi paper-mode fallback was removed; explicit demo mode
  replaces it.
- Legacy `collect`/`watch` take `--mode` and point at `replay` in fixture
  mode.
