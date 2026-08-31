# Spike 4 Results: SQLGlot-direct core — ✅ GREEN

**Date:** 2026-08-31 · **Versions:** sqlglot 30.8.0, duckdb 1.5.5, pyiceberg 0.11.1
**Verdict:** Everything Reble uses SQLMesh for is covered by SQLGlot + ~100 lines of
our own code — and the architecture gets simpler, not just lighter. The slim runner
in this spike is ~20 lines and passes the full branch loop against the real
BranchEngine with **no SQLMesh import anywhere**. Run `spike.py` to reproduce.

## What was verified

| # | Capability | Result | How |
|---|-----------|--------|-----|
| 1 | Dependency extraction (CTEs correctly excluded) | ✅ | `parse_one().find_all(exp.Table)` minus CTE aliases |
| 2 | Topological execution order | ✅ | ~15-line DFS over the dep graph |
| 3 | Column-level lineage | ✅ | `sqlglot.lineage("total_amount", sql, schema=...)` → walked to `orders_clean.amount` |
| 4 | Change-detection fingerprints | ✅ | canonical SQL (comments/whitespace/case stripped) + composite upstream hash: cosmetic edit → same hash; semantic edit → new hash; upstream edit → cascades; unrelated model → stable |
| 5 | Slim runner end-to-end | ✅ | ephemeral in-memory DuckDB; branch-resolved Iceberg scans registered as views; outputs committed straight to branch refs; **epoch held** (prod ingested post-branch, branch computed 25.0 not 930.0-era totals) and **main untouched** |

## What this eliminates from the current architecture

- The **second isolation system** (SQLMesh virtual environments in `.reble/db.db`)
- The **mirror step** (Iceberg → DuckDB copy before every run) and the
  **publish copy-back** — data now flows Iceberg → Arrow views → DuckDB → Iceberg once
- The **persistent `.reble/db.db`** file entirely (runs use throwaway in-memory sessions)
- The **fingerprint bridge** (`published` table reconciling SQLMesh snapshots with
  Iceberg state) — our composite hash is the only fingerprint
- The pin on `sqlmesh==0.236.1` and reliance on its non-public `core.lineage`

## What we give up (and when it matters)

- **Incremental models** — v0.2+ concern; current core is FULL-only anyway, and at
  the ≤50GB target full rebuilds are seconds (spike 2)
- **SQLMesh project compatibility** — becomes an importer (`MODEL(...)` header strip),
  not a core dependency; same treatment as a future dbt importer (`{{ ref() }}` →
  table names, then SQLGlot handles the rest)

## Model format unlocked

```sql
-- models/demo/orders_clean.sql      ← filename = table name, no header, no Jinja
SELECT id, amount FROM raw.orders WHERE amount > 0
```

Dependencies inferred from the SQL itself. "Your models are just SQL files."

## Migration plan (next work)

1. New `runner.py` on the slim core (parse/deps/fingerprints/topo/execute)
2. Scope inference + pins from `deps_of` (drop `analyze_project`'s SQLMesh plan)
3. Keep all 25 tests green against the new runner; add fingerprint-cascade tests
4. Remove `sqlmesh` from pyproject; delete mirror/publish bridge code
5. Docs/PRD: positioning becomes "built on SQLGlot"; SQLMesh moves to the importer
   roadmap; spike 3 stays as historical record
