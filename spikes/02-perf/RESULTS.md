# Spike 2 Results: compute-path performance — ✅ GREEN at full 10GB scale

**Date:** 2026-08-30 · **Hardware:** Apple M4 Pro, 24GB RAM
**Versions:** pyiceberg 0.11.1, duckdb 1.5.5, pyarrow 25.0.1
Run twice: a scaled run (20M rows) and the full-target run (**140M rows, 10.22GB in
Arrow**). Reproduce with `spike.py 28`.

## Measured

| Operation | 20M rows (1.5GB) | **140M rows (10.22GB)** |
|---|---|---|
| Bulk load (Arrow → Parquet commits) | 6.1s | 41.6s (~3.5M rows/s end-to-end) |
| **Branch create (zero-copy claim)** | <10ms | **<10ms — size-independent, confirmed** |
| Pinned full scan → Arrow | 0.33s | **4.0s** |
| Projected scan (2 of 6 columns) | 0.03s | 0.47s (2.27GB vs 10.22GB — 4.5× lighter) |
| Group-by aggregation (DuckDB) | 0.02s | 0.25s |
| Branch append (5M rows) | 0.70s | 1.32s |
| **Full diff (scan both refs + anti-join + changed)** | 0.64s | **5.9s** |
| Peak RSS | 4.5GB | 12.3GB |
| Warehouse on disk | 0.6GB | 3.5GB (Parquet ≈ 2.9× compression) |

## Conclusions

1. **The N3 scale target (correct and pleasant at ≤~50GB warehouse on a laptop) is
   validated at the per-table 10GB level, measured — not extrapolated.** Every
   interactive operation lands in single-digit seconds.
2. **Zero-copy branching is size-independent**: <10ms at 140M rows, same as at 150
   rows. Safe to advertise.
3. **RAM, not time, is the constraint.** A full 10GB materialization peaks at ~12GB
   RSS. The mitigation is measured: projection to needed columns cut memory 4.5× on
   this schema. Design rules for the build:
   - lineage-driven `selected_fields` on **every** scan (model inputs and diffs);
   - diff reads key + compared columns only, never whole rows;
   - DuckDB gets `memory_limit` + `temp_directory` so big joins spill, not OOM;
   - release full-table Arrow references as soon as the consumer is done.
4. Bulk-load throughput (~3.5M rows/s including data generation) means even a full
   warehouse rebuild at target scale is minutes, not hours.

## Reproduce

```bash
python -m venv .venv
.venv/bin/pip install "pyiceberg[sql-sqlite,pyarrow]" duckdb
.venv/bin/python spike.py 28   # full 10GB run; ~4GB temp disk, self-cleaning
```
