# Spike 07 — local branches over remote main

**Question:** can a developer with *read-only* access to the shared warehouse
hold a zero-copy data branch entirely on their own machine?

**Why it matters:** it makes team mode work like git — branches are local and
private, shared main is protected, and "individuals can't write prod" is
enforced by bucket credentials, not convention. The only writer to shared
main is the merge gate (CI running `reble run` on main after a PR merges).

**Mechanism:** pyiceberg `add_files` registers the remote table's Parquet
files (from a pinned snapshot's scan plan) into a table in a *local* catalog
— no bytes copied. Local writes then land only in the local warehouse.

## Measured (1,000,000-row table, pyiceberg 0.11.1, local disk)

| Step | Result |
|---|---|
| Branch: `add_files` of remote snapshot into local catalog | **6ms**, zero-copy (no Parquet in local warehouse) |
| Read parity: local overlay scan vs remote pinned scan | identical rows and sums |
| Local overwrite (1,000,000 → 857,240 rows) | 0.05s, new files local only |
| Remote warehouse after branch + write | **bit-identical** (size+sha256 of every file) |
| Cross-catalog diff (local branch vs remote pinned base) | −142,760 detected in 0.03s |

## Conclusion

Viable. The branch resolver routes scoped tables to the local catalog and
unscoped reads to pinned remote snapshots — the same adapter seam that
already exists. Remaining engineering (not spiked here): wiring this as a
`mode: team` config, credential documentation, and snapshot-expiry safety
(a pinned remote snapshot could be garbage-collected under a long-lived
local branch; needs a retention convention or re-pin).

Run it: `python spike.py` (writes to `work/`, self-contained).
