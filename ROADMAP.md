# Roadmap

Directions under consideration. Nothing on this page is implemented or
claimed as working today; the README documents only what the code does now.

## Planned / under consideration

- **Explicit fixture / demo / live modes.** One documented switch, with
  fixture the default and live refusing to start without the required
  credentials and a clear statement of what is missing.
- **A careful live pilot.** Rate limiting, backoff with jitter, snapshot
  provenance, and a digest that separates fixture, demo, and live sections
  so the three can never be confused.
- **Automated pair-matching assistance.** Candidate suggestions for
  cross-venue market matches, with manual confirmation still required
  before any pair is monitored.
- **SQLite schema migrations.** Versioned migrations instead of
  create-if-missing, so existing databases upgrade cleanly.

## Explicitly out of scope

- Order placement, execution, or any write path to a venue. pmwatch is
  read-only and will stay that way.
- Trading advice, signals, or recommendations of any kind.
