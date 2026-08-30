# Spike 1 Results: pyiceberg branch refs — ✅ GREEN

**Date:** 2026-08-30 · **Versions:** pyiceberg 0.11.1, duckdb 1.5.5, pyarrow 25.0.1, Python 3.14.7
**Verdict:** Every operation Reble's branch model needs works today, out of the box.
Run `spike.py` to reproduce (all 7 checks pass).

## What was verified

| # | Operation | Result | API |
|---|-----------|--------|-----|
| 1 | Zero-services local setup (SQLite catalog + filesystem warehouse) | ✅ | `load_catalog(type="sql", uri="sqlite:///...", warehouse="file://...")` |
| 2 | Create branch ref at a snapshot (zero-copy) | ✅ | `tbl.manage_snapshots().create_branch(snap_id, name).commit()` |
| 3 | Write to a branch; main unaffected | ✅ | `tbl.append(df, branch="pr-1")` |
| 4 | Write to main; branch unaffected | ✅ | bidirectional isolation confirmed |
| 5 | Pinned snapshot read while main advances | ✅ | `tbl.scan(snapshot_id=s1)` |
| 6 | Promote (move main to branch's snapshot) | ✅ | `manage_snapshots().set_current_snapshot(ref_name="pr-1")` — takes `snapshot_id` or `ref_name` |
| 7 | Branch cleanup | ✅ | `manage_snapshots().remove_branch(name)` |
| 8 | DuckDB queries branch data via Arrow | ✅ | `tbl.scan(snapshot_id=...).to_arrow()` → `duckdb.register()` |

## Notes for the build

- **No native `fast_forward` API** — promote is `set_current_snapshot`. It moves the
  ref unconditionally, so Reble must implement the "clean vs. dirty" check itself:
  compare main's current snapshot against the branch's recorded parent; if main moved,
  refuse and fall back to re-apply. (This was the plan anyway — the check is ours.)
- **Branch reads use `scan(snapshot_id=refs[name].snapshot_id)`** — resolve the ref to
  a snapshot at read time; there's no `scan(ref=...)` param in 0.11.1.
- **Pin dependency:** `pyiceberg==0.11.1`. Branch-write (`append(branch=...)`) is the
  newest of these APIs; re-run this spike on any upgrade.

## Remaining spikes

- Spike 2: pinned-snapshot read performance at ~10GB (correctness proven here;
  performance not yet).
- Spike 3: SQLMesh Python-API embedding (plan/apply driven programmatically, with
  DuckDB compute → pyiceberg commit as the write path).
