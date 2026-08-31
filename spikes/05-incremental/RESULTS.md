# Spike 5 Results: incremental models on branches — ✅ GREEN

**Date:** 2026-08-31 · **Versions:** pyiceberg 0.11.1 (native upsert), duckdb 1.5.5
**Verdict:** every assumption in the v0.2 incremental design holds against the real
BranchEngine. The branch machinery (refs, epoch pins, clean/dirty promote) extends
to incremental models **without modification**. Run `spike.py` to reproduce.

## Assumptions verified

| # | Assumption | Result |
|---|-----------|--------|
| 1 | Watermarks derivable per-ref from data (`max(ts)` on the ref); main and branch advance independently | ✅ main=3 while branch=2 |
| 2 | Branch increments read EPOCH-PINNED inputs (post-epoch prod ingest invisible), and reruns are idempotent no-ops | ✅ |
| 3 | `pyiceberg upsert(join_cols=..., branch=...)` merges by key onto a branch ref, main untouched | ✅ 1 updated + 1 inserted on branch; main value intact |
| 4 | Fingerprint-changed ⇒ full rebuild on the branch restates history — all shared rows changed, rebuild covers the full pinned input, main untouched (this is the diffable-restatement review artifact) | ✅ |
| 5 | Promote semantics survive: main's own incremental run after branching ⇒ promote **refused** (dirty); clean case fast-forwards and carries incremental history | ✅ both |
| 6 | Watermark caching via `snapshot_properties={"reble.watermark": ...}` round-trips on the snapshot summary | ✅ |

## Notes for the v0.2 build

- The incremental executor in this spike is ~10 lines (watermark query → filter
  pinned input → guarded append). Upsert mode is one call.
- Spike 4's assertion lesson repeated here in miniature: an early FAIL was a wrong
  *expectation*, not wrong behavior — the rebuild rightly covers input rows that
  main's stale output never processed. Encode that in the real tests.
- Config surface stays as designed: `kind: incremental` + `time_column` (append
  mode) or `unique_key` (upsert mode) in reble.yml's `models:` section.
- Derive watermark from data by default (projected scan, ~free); snapshot-property
  cache is an optimization, verified available.
