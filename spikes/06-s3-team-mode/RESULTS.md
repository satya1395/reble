# Spike 6 Results: team mode over S3 — ✅ GREEN (MinIO + real AWS S3)

**Date:** 2026-09-01 · **Versions:** pyiceberg 0.11.1, pyarrow 25.0.1
**Verdict:** the full loop — write, zero-copy branch, epoch pins, isolated
branch writes, diff, promote — passes over real object storage. Run against
MinIO (local, credential-free) and a real AWS S3 bucket (us-east-1, created and
deleted for the test). Reproduce: `spike.py s3://bucket/prefix [--endpoint ...]`.

## Findings

1. **Config gap found & fixed**: reble.yml had no way to pass S3
   endpoint/credential properties to pyiceberg — team mode was
   config-INcomplete until this spike. New `catalog_properties:` key passes
   straight through to `load_catalog` (e.g. `s3.endpoint` for MinIO/R2); on
   AWS the standard credential chain works with no config at all.
2. **All branch semantics hold on S3** — refs, pins, isolation, promote are
   catalog/metadata operations and behave identically.

## Latency context (tiny data — this is per-op overhead, not throughput)

| Operation | MinIO (localhost) | real S3 (us-east-1) | local NVMe (spike 2) |
|---|---|---|---|
| branch create | 14ms | 632ms | <10ms |
| table read (3 rows) | 17ms | 322ms | — |
| branch overwrite | 31ms | 1.8s | — |
| diff | 36ms | 507ms | 0.64s @20M rows |
| promote | 21ms | 1.1s | — |

Interpretation: S3 metadata round-trips put every operation in the
hundreds-of-ms to ~1s band — fine for interactive team use, and the data-scan
design rules (projection pushdown) carry over. Large-scan throughput on S3
remains unmeasured; do not quote spike 2's numbers for S3.

## Still open for team mode (F11)

- Shared **catalog** (Postgres SQL catalog / REST) — this spike used a local
  SQLite catalog with S3 data; the shared-catalog swap is a URI change but
  concurrent multi-writer behavior is untested.
- Large-scan S3 throughput at the 10GB scale.
