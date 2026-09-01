# Spike 08 — off-the-shelf clients through `reble serve`: PASS

All exit criteria met (pyiceberg 0.11.1, DuckDB 1.5.5, iceberg extension):

| Criterion | Result |
|---|---|
| pyiceberg REST client reads the **branch's** rows | ✓ (`branch-view`) |
| DuckDB `ATTACH … (TYPE iceberg, ENDPOINT …)` reads the branch's rows | ✓ |
| Pinned table shows **pinned** rows (3) while main has newer (5) | ✓ both clients |
| Write attempt through the proxy | ✓ 405, read-only message |
| On main, the proxy serves current main | ✓ (test suite) |

## Answers to the design doc's open questions

1. **DuckDB attach incantation** (min version: the iceberg extension for
   DuckDB 1.5.x):
   ```sql
   INSTALL iceberg; LOAD iceberg;
   ATTACH 'warehouse' AS wh
     (TYPE iceberg, ENDPOINT 'http://127.0.0.1:8181',
      AUTHORIZATION_TYPE 'none');
   ```
2. **Inline metadata is NOT sufficient on its own** — the real finding of
   this spike: DuckDB's REST attach resolves the current snapshot from the
   **tail of `snapshot-log`**, not from `current-snapshot-id`. Branch-ref
   commits don't advance the snapshot-log, so a passthrough log pointed
   clients at stale (even empty) snapshots. The synthesis therefore
   rewrites `snapshot-log` to a single entry agreeing with the resolved
   snapshot (regression-tested). pyiceberg honors `current-snapshot-id`
   directly.
3. **Credentials:** local warehouses need none; for S3 warehouses the
   client brings its own AWS credentials (vended credentials deferred, as
   planned).

Run it: `python spike.py` (self-contained; writes to `work/`).
