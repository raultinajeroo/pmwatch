# Bundled fixtures

Handcrafted, fixture-derived sample data for the offline demo. These are
not observations of live markets; every artifact produced from them says
so in its footer.

Two matched pairs, 12 timesteps each on 2026-07-30:

- `fed-cut-sep-2026/` — plants a persistent 2.3c cross-venue arb for 6
  snapshots and then closes. Exercises the arb and divergence detectors.
- `us-recession-2026/` — stays aligned. Exercises the no-dislocation path.

Run the demo offline (no API keys, no network):

```bash
pmwatch replay --fixtures fixtures/ --db /tmp/pmwatch_replay.db
pmwatch report --db /tmp/pmwatch_replay.db --date 2026-07-30
```
