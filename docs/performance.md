# Performance

Every number on this page is **measured, reproducible, and honest about its
limits** — laptop hardware, list-price regions, no clusters. Run the
commands yourself; scripts are in the repo.

## Is DuckDB enough?

For the transformation job — building and diffing mart-sized tables — the
measured answer is yes, at sizes well past a small team's warehouse:

- **Branching is metadata-only.** Creating a branch on a 5M-row table took
  **< 10 ms** — measured, repeatedly. It costs nothing because nothing is
  copied.
- **Reads stream, they don't materialize.** Run inputs and diffs go through
  duckdb's `iceberg_scan` — out-of-core, spilling to disk under a
  configurable `memory_limit`. Working-set size is bounded by config, not
  by your RAM.
- **Diffs are keyed SQL, not data movement.** A keyed diff of a 1M-row
  table over S3 runs in ~4 s from a laptop.

Where DuckDB ends — single-node joins that exceed memory even with spill,
warehouse-concurrent serving — is where the Spark runner (same engine
interface) takes over. Nobody has hit that line with us yet; when someone
does, it's a config change, not a migration.

## Measured: full lifecycle on AWS (Glue + S3, us-east-1, laptop)

1M-row input table, two models, disposable bucket. Reproduce with
`python -m reble.aws_smoke --pass-1m` (creates and cleans up its own
bucket + Glue database; asserts streaming reads engaged — the run fails if
a read falls back to in-memory).

| Phase | Time |
| --- | --- |
| Seed 100k rows | ~3 s |
| First run (2 models) | ~13 s |
| Edited scoped re-run | ~8 s |
| Keyed diff (100k→1M rows) | ~4 s |
| Drift detection (`status`) | ~2 s |
| Promote with forced re-run | ~15 s |
| Append 1M rows (4 chunks) | ~8 s |
| `--refresh` over 1.1M rows | ~13 s |

Streaming verification: **zero `iceberg_scan` fallbacks** across every
phase, both runs.

## Measured: local warehouse, 5M rows

Synthetic project on a sqlite catalog + local disk (M-series laptop,
`memory_limit: 2GB`). Reproduce with `benchmarks/duckdb_scale.py`.

| Phase | arrow mode | streaming mode (`auto`) |
| --- | --- | --- |
| Branch create (metadata) | < 10 ms | < 10 ms |
| First run (2 models) | 0.9 s | 0.9 s |
| Edited re-run | 0.7 s | 0.7 s |
| Keyed diff | 0.09 s | 0.12 s |

Both modes produce **identical diff results** (verified row-for-row) —
the streaming path's value is memory bounding, not wall time at laptop
scale.

## Method & caveats

- Hardware: Apple silicon laptop, 2 GB duckdb memory limit for the local
  benchmark. AWS runs from the same machine, us-east-1.
- Sizes: 100k–5M rows, 2–3 models. Not a TPC benchmark; it's the real
  product doing its real verbs.
- Your numbers will vary with region, network, and table width. The shape
  is the claim: laptop, no cluster, real warehouse sizes, honest timings.
