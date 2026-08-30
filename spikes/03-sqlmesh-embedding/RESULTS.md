# Spike 3 Results: SQLMesh Python-API embedding — ✅ GREEN

**Date:** 2026-08-30 · **Versions:** sqlmesh 0.236.1, pyiceberg 0.11.1, Python 3.14.7
**Verdict:** SQLMesh can be driven entirely from its Python API — no CLI, no prompts —
and its output hands off to an Iceberg branch cleanly. The last of the two hard
technical spikes is retired. Run `spike.py` to reproduce (all 7 checks pass).

## What was verified

| # | Operation | Result | API |
|---|-----------|--------|-----|
| 1 | Load project programmatically | ✅ | `Context(paths=[...])` |
| 2 | plan/apply to prod, no prompts | ✅ | `ctx.plan(auto_apply=True, no_prompts=True)` |
| 3 | Fetch materialized output as Arrow | ✅ | `ctx.fetchdf(sql)` → `pa.Table.from_pandas` |
| 4 | Isolated dev environment (SQLMesh half of a reble branch) | ✅ | `ctx.plan(environment="dev", ..., include_unmodified=True)` → `demo__dev` schema |
| 5 | Column-level lineage | ✅ | `sqlmesh.core.lineage.lineage(column, model)` — walked `total_amount` → `orders.amount`; also `column_dependencies` in module |
| 6 | Change detection | ✅ | edited 1 of 2 models; plan's `new_snapshots` contained exactly that model |
| 7 | Full Reble loop in miniature | ✅ | SQLMesh output (Arrow) → `pyiceberg append(branch="pr-1")` — main and branch verified independent |

## Notes for the build

- **The write path is confirmed as designed:** SQLMesh/DuckDB computes → results come
  back as Arrow → pyiceberg commits to the branch ref. No dependency on DuckDB writing
  Iceberg, no SQLMesh engine-adapter surgery needed for v0.1.
- **reble branch ↔ SQLMesh environment (1:1) is directly supported**: environments are
  a first-class `plan()` parameter; `include_unmodified=True` materializes the full
  view layer for an environment.
- **Lineage needs no parsing of ours**: `lineage()` returns a walkable node graph per
  column. For the impact side (who reads this column downstream), invert by walking
  all models' columns once and indexing — cheap at ≤200-model scale.
- **Change detection is plan-native**: `plan.new_snapshots` is the changed-model set;
  this also powers scope inference for `branch create` (F12).
- **Pin `sqlmesh==0.236.1`.** `sqlmesh.core.lineage` is not a documented public
  surface — re-run this spike on any upgrade (same policy as pyiceberg).

## Remaining spike

- Spike 2 (performance): pinned-snapshot reads + branch writes at ~10GB. Correctness
  is proven; scale behavior on a laptop is not. Lower risk than the two now retired —
  it bounds N3 (scale target) rather than gating feasibility.
