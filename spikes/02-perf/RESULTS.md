# Spike 2 Results: compute-path performance — ✅ GREEN (scaled run)

**Date:** 2026-08-30 · **Hardware:** Apple M4 Pro, 24GB RAM
**Versions:** pyiceberg 0.11.1, duckdb 1.5.5, pyarrow 25.0.1
**Scale caveat:** designed for 10GB; run at **20M rows / ~1.5GB Arrow** because the
host machine had only 5GB free disk. Numbers below extrapolate roughly linearly;
re-run at full 10GB (machine with headroom, or CI) before freezing the N3 scale target.

## Measured (20M-row table, 25M rows total written)

| Operation | Time |
|---|---|
| Bulk append (per 5M-row batch, Arrow → Parquet commit) | ~0.7s (~7M rows/s) |
| **Branch create on 20M-row table (zero-copy claim)** | **< 10ms** |
| Pinned full scan → Arrow (1.46GB) | 0.33s |
| Projected scan, 2 of 6 columns (0.33GB) | 0.03s |
| Group-by aggregation over 20M rows (DuckDB) | 0.02s |
| Branch append (5M rows to branch ref) | 0.70s |
| **Full diff: scan both refs + anti-join + changed-row detection** | **0.64s** |

Peak RSS: 4.5GB · Warehouse on disk: 0.6GB (Parquet ≈ 2.4× compression on this data)

## Extrapolation to the 10GB target (~7×)

- Full scan ~2–3s, full diff ~5s, bulk load ~45s — all acceptable.
- **The 10GB constraint is memory, not time.** Fully materializing a wide 10GB table
  → ~25GB+ RSS, over a 24GB laptop. Mitigation is measured here: the projected scan
  was 4.4× smaller reading 2 of 6 columns.

## Design rules carried into the build

1. **Projection pushdown always.** Every scan (model inputs, diffs) passes
   `selected_fields` derived from lineage — never full-materialize a wide table when
   the consumer needs a subset. This is what keeps 10GB workloads inside laptop RAM.
2. **Diff on key + compared columns only** (as this spike did: `id`, `amount`), not
   whole rows.
3. Set DuckDB `memory_limit` + `temp_directory` so large joins spill instead of OOM.
4. Zero-copy branch creation is confirmed independent of table size — safe to
   advertise.

## Reproduce

```bash
python -m venv .venv
.venv/bin/pip install "pyiceberg[sql-sqlite,pyarrow]" duckdb
.venv/bin/python spike.py   # generates ~0.6GB temp data, cleans up after itself
```
