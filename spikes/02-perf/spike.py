"""Spike 2: performance of the Reble compute path.

Usage: spike.py [BATCHES]   (default 4 ≈ 1.5GB quick run; 28 ≈ 10GB full run)
Validated at both scales on 2026-08-30 — see RESULTS.md. Needs ~2x the target
size in free disk (Parquet + DuckDB temp); cleans up after itself.

Measures:
  1. Bulk append throughput (DuckDB-generated Arrow -> pyiceberg parquet commit)
  2. Branch creation time on a large table (the zero-copy claim)
  3. Pinned full-table scan -> Arrow (time + peak RSS)
  4. Projected scan (column pruning: just id+amount)
  5. Aggregation over the scan in DuckDB
  6. Branch write (append batch to branch ref)
  7. Diff: anti-join branch vs main + changed-row count in DuckDB
  8. Disk footprint of the warehouse; cleans up after itself
"""

import resource
import shutil
import sys
import time
from pathlib import Path

import duckdb
import pyarrow as pa
from pyiceberg.catalog import load_catalog

HERE = Path(__file__).parent
WH = HERE / "warehouse"
BATCHES = int(sys.argv[1]) if len(sys.argv) > 1 else 4   # 28 ≈ 10GB in Arrow
ROWS_PER_BATCH = 5_000_000


def rss_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9


def gen_batch(con, offset: int) -> pa.Table:
    return con.execute(f"""
        SELECT (range + {offset})::BIGINT              AS id,
               (hash(range) % 100000)::INT             AS customer_id,
               (TIMESTAMP '2026-01-01' + INTERVAL ((range % 365)::INT) DAY) AS ts,
               round((hash(range + 7) % 100000) / 100.0, 2) AS amount,
               ['new','paid','shipped','done'][1 + range % 4] AS status,
               md5(range::VARCHAR)                     AS note
        FROM range({ROWS_PER_BATCH})
    """).to_arrow_table()


def main() -> int:
    shutil.rmtree(WH, ignore_errors=True)
    WH.mkdir(parents=True)
    gen = duckdb.connect()
    t = {}

    cat = load_catalog("local", type="sql",
                       uri=f"sqlite:///{WH}/cat.db", warehouse=f"file://{WH}")
    cat.create_namespace("perf")

    # --- 1. bulk load main ---
    tbl = None
    t0 = time.time()
    for i in range(BATCHES):
        batch = gen_batch(gen, i * ROWS_PER_BATCH)
        if tbl is None:
            tbl = cat.create_table("perf.orders", schema=batch.schema)
        bt = time.time()
        tbl.append(batch)
        print(f"  batch {i+1}/{BATCHES}: {ROWS_PER_BATCH:,} rows appended "
              f"in {time.time()-bt:.1f}s", flush=True)
    t[f"1. bulk load ({BATCHES}x5M rows)"] = time.time() - t0
    total_rows = BATCHES * ROWS_PER_BATCH

    # --- 2. branch creation (zero-copy claim) ---
    s1 = tbl.current_snapshot().snapshot_id
    t0 = time.time()
    tbl.manage_snapshots().create_branch(s1, "pr-1").commit()
    t[f"2. branch create on {total_rows//1_000_000}M-row table"] = time.time() - t0

    # --- 3. pinned full scan -> Arrow ---
    t0 = time.time()
    full = tbl.scan(snapshot_id=s1).to_arrow()
    t["3. pinned FULL scan -> Arrow"] = time.time() - t0
    assert full.num_rows == total_rows
    print(f"  full scan: {full.num_rows:,} rows, {full.nbytes/1e9:.2f} GB in Arrow, "
          f"peak RSS {rss_gb():.1f} GB", flush=True)

    # --- 4. projected scan (column pruning) ---
    t0 = time.time()
    proj = tbl.scan(snapshot_id=s1, selected_fields=("id", "amount")).to_arrow()
    t["4. projected scan (id, amount only)"] = time.time() - t0
    print(f"  projected: {proj.nbytes/1e9:.2f} GB in Arrow", flush=True)

    # --- 5. aggregation in DuckDB over the Arrow table ---
    con = duckdb.connect(config={"memory_limit": "12GB",
                                 "temp_directory": str(WH / "ddb_tmp")})
    con.register("orders", full)
    t0 = time.time()
    con.execute("SELECT status, count(*), sum(amount) FROM orders GROUP BY status").fetchall()
    t[f"5. group-by agg over {total_rows//1_000_000}M rows (DuckDB)"] = time.time() - t0

    con.unregister("orders")
    del full, proj   # release before diff so peak RSS reflects the real diff path

    # --- 6. branch write (1 batch onto the branch ref) ---
    upd = gen_batch(gen, BATCHES * ROWS_PER_BATCH)
    t0 = time.time()
    tbl.append(upd, branch="pr-1")
    t["6. branch append (5M rows)"] = time.time() - t0

    # --- 7. diff: branch vs main (anti-join + changed rows) ---
    tbl = cat.load_table("perf.orders")
    br_snap = tbl.metadata.refs["pr-1"].snapshot_id
    t0 = time.time()
    b = tbl.scan(snapshot_id=br_snap, selected_fields=("id", "amount")).to_arrow()
    m = tbl.scan(snapshot_id=s1, selected_fields=("id", "amount")).to_arrow()
    con.register("b", b)
    con.register("m", m)
    added = con.execute("SELECT count(*) FROM b ANTI JOIN m USING (id)").fetchone()[0]
    changed = con.execute(
        "SELECT count(*) FROM b JOIN m USING (id) WHERE b.amount <> m.amount"
    ).fetchone()[0]
    t["7. full diff (scan both refs + anti-join + changed)"] = time.time() - t0
    print(f"  diff: {added:,} added, {changed:,} changed", flush=True)

    # --- 8. footprint ---
    disk = sum(f.stat().st_size for f in WH.rglob("*") if f.is_file()) / 1e9
    print(f"\n  warehouse on disk: {disk:.2f} GB "
          f"({total_rows + ROWS_PER_BATCH:,} total rows) | peak RSS {rss_gb():.1f} GB")

    print("\n--- TIMINGS ---")
    for k, v in t.items():
        print(f"  {v:8.2f}s  {k}")

    shutil.rmtree(WH, ignore_errors=True)   # leave the disk as we found it
    print("\ncleaned up warehouse dir")
    return 0


if __name__ == "__main__":
    sys.exit(main())
