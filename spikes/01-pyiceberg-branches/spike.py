"""Spike 1: Can pyiceberg do what Reble's branch model needs?

Tests, in order:
  1. Local setup: SQLite catalog + filesystem warehouse, create table, append (main)
  2. Create a branch ref at the current snapshot (zero-copy)
  3. Write to the branch; verify main is unchanged (isolation, branch->main)
  4. Write to main; verify branch is unchanged (isolation, main->branch)
  5. Read a pinned snapshot explicitly (time travel for pins)
  6. Fast-forward main to the branch (promote) -- introspect what API exists
  7. DuckDB reads an iceberg scan via Arrow (the compute path)
"""

import inspect
import shutil
import sys
from pathlib import Path

import duckdb
import pyarrow as pa
from pyiceberg.catalog import load_catalog

BASE = Path(__file__).parent / "warehouse"
RESULTS: list[tuple[str, str]] = []


def report(step: str, ok: bool, detail: str = ""):
    status = "PASS" if ok else "FAIL"
    RESULTS.append((status, step))
    print(f"[{status}] {step}" + (f" -- {detail}" if detail else ""))


def batch(start: int, n: int) -> pa.Table:
    return pa.table({
        "id": pa.array(range(start, start + n), pa.int64()),
        "amount": pa.array([float(i) * 1.5 for i in range(start, start + n)], pa.float64()),
    })


def main() -> int:
    if BASE.exists():
        shutil.rmtree(BASE)
    BASE.mkdir(parents=True)

    # --- 1. Local-first setup: SQLite catalog, filesystem warehouse ---
    try:
        catalog = load_catalog(
            "local",
            type="sql",
            uri=f"sqlite:///{BASE}/catalog.db",
            warehouse=f"file://{BASE}",
        )
        catalog.create_namespace("analytics")
        tbl = catalog.create_table("analytics.orders", schema=batch(0, 1).schema)
        tbl.append(batch(0, 100))                      # snapshot S1 on main
        s1 = tbl.current_snapshot().snapshot_id
        report("1. SQLite catalog + create table + append on main", True, f"S1={s1}")
    except Exception as e:
        report("1. SQLite catalog + create table + append on main", False, repr(e))
        return 1

    # --- 2. Create branch ref at S1 (zero-copy) ---
    try:
        tbl.manage_snapshots().create_branch(s1, "pr-1").commit()
        refs = tbl.refresh().metadata.refs
        assert "pr-1" in refs and refs["pr-1"].snapshot_id == s1, refs
        report("2. Create branch ref 'pr-1' at S1", True, f"refs={list(refs)}")
    except Exception as e:
        report("2. Create branch ref 'pr-1' at S1", False, repr(e))
        return 1

    # --- 3. Write to branch; main unchanged ---
    try:
        sig = inspect.signature(tbl.append)
        if "branch" not in sig.parameters:
            report("3. Branch-targeted append", False,
                   f"append() has no branch param; signature={sig}")
        else:
            tbl.append(batch(100, 50), branch="pr-1")
            tbl = catalog.load_table("analytics.orders")
            main_rows = tbl.scan().to_arrow().num_rows          # main ref
            branch_snap = tbl.metadata.refs["pr-1"].snapshot_id
            branch_rows = tbl.scan(snapshot_id=branch_snap).to_arrow().num_rows
            ok = main_rows == 100 and branch_rows == 150
            report("3. Write to branch; main unchanged", ok,
                   f"main={main_rows} rows, pr-1={branch_rows} rows")
    except Exception as e:
        report("3. Write to branch; main unchanged", False, repr(e))

    # --- 4. Write to main; branch unchanged ---
    try:
        tbl.append(batch(1000, 25))                    # main moves to S3
        tbl = catalog.load_table("analytics.orders")
        main_rows = tbl.scan().to_arrow().num_rows
        branch_snap = tbl.metadata.refs["pr-1"].snapshot_id
        branch_rows = tbl.scan(snapshot_id=branch_snap).to_arrow().num_rows
        ok = main_rows == 125 and branch_rows == 150
        report("4. Write to main; branch unchanged", ok,
               f"main={main_rows} rows, pr-1={branch_rows} rows")
    except Exception as e:
        report("4. Write to main; branch unchanged", False, repr(e))

    # --- 5. Pinned snapshot read (the 'pins' half of the branch model) ---
    try:
        pinned_rows = tbl.scan(snapshot_id=s1).to_arrow().num_rows
        report("5. Read pinned snapshot S1 while main has moved", pinned_rows == 100,
               f"pinned={pinned_rows} rows (expect 100)")
    except Exception as e:
        report("5. Read pinned snapshot S1 while main has moved", False, repr(e))

    # --- 6. Promote: what fast-forward APIs exist? ---
    try:
        ms = tbl.manage_snapshots()
        api = [m for m in dir(ms) if not m.startswith("_")]
        print(f"    manage_snapshots API: {api}")
        # main moved since branch creation, so true fast-forward must refuse;
        # this tells us which primitive promote gets built on.
        candidates = [m for m in api if "fast" in m or "ref" in m or "branch" in m]
        report("6. Promote-primitive discovery", True, f"candidates={candidates}")
    except Exception as e:
        report("6. Promote-primitive discovery", False, repr(e))

    # --- 7. DuckDB reads branch data via Arrow (compute path) ---
    try:
        branch_snap = tbl.metadata.refs["pr-1"].snapshot_id
        arrow_tbl = tbl.scan(snapshot_id=branch_snap).to_arrow()
        con = duckdb.connect()
        con.register("orders", arrow_tbl)
        n, total = con.execute("SELECT count(*), sum(amount) FROM orders").fetchone()
        report("7. DuckDB query over branch scan via Arrow", n == 150,
               f"count={n}, sum(amount)={total:.1f}")
    except Exception as e:
        report("7. DuckDB query over branch scan via Arrow", False, repr(e))

    print("\n--- SUMMARY ---")
    for status, step in RESULTS:
        print(f"  {status}: {step}")
    return 0 if all(s == "PASS" for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
