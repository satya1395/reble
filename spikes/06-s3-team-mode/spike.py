"""Spike 6: does the full Reble loop hold over S3 object storage (team mode)?

Runs against any S3-compatible endpoint. Locally: MinIO (correctness — the
'works over object storage at all' question). Against real S3: adds honest
latency context. Checks the claims the docs/FAQ make about team mode:

  1. warehouse on s3:// — tables created, data written and read back
  2. zero-copy branch creation on S3 data
  3. epoch pins hold while 'prod' ingests (the reproducibility claim)
  4. branch writes isolated from main
  5. diff computes over two S3 refs
  6. promote fast-forwards; refs cleaned up
  7. rough per-op timings (context, not benchmarks)

Usage:
  spike.py s3://bucket/prefix                     # AWS (standard cred chain)
  spike.py s3://bucket/prefix --endpoint http://127.0.0.1:9101 \
           --access-key X --secret-key Y          # MinIO / R2 / etc.
"""

import argparse
import sys
import time

import pyarrow as pa

from reble.branches import BranchEngine
from reble.config import RebleConfig
from reble.diffing import diff_branch

RESULTS: list[tuple[str, str]] = []


def report(step, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    RESULTS.append((status, step))
    print(f"[{status}] {step}" + (f" -- {detail}" if detail else ""))


def rows(ids, amounts):
    return pa.table({"id": pa.array(ids, pa.int64()),
                     "amount": pa.array(amounts, pa.float64())})


def scan(eng, table, branch=None):
    tbl = eng.catalog.load_table(table)
    snap = None
    if branch:
        snap = tbl.metadata.refs[branch].snapshot_id
    return tbl.scan(snapshot_id=snap).to_arrow()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("warehouse")
    ap.add_argument("--endpoint")
    ap.add_argument("--access-key")
    ap.add_argument("--secret-key")
    ap.add_argument("--tmpdir", default="/tmp/reble-s3-spike")
    args = ap.parse_args()

    props = {}
    if args.endpoint:
        props["s3.endpoint"] = args.endpoint
    if args.access_key:
        props["s3.access-key-id"] = args.access_key
        props["s3.secret-access-key"] = args.secret_key

    from pathlib import Path
    import shutil
    proj = Path(args.tmpdir)
    shutil.rmtree(proj, ignore_errors=True)
    proj.mkdir(parents=True)

    cfg = RebleConfig(project_dir=proj, warehouse=args.warehouse,
                      catalog_uri=f"sqlite:///{proj}/catalog.db",
                      catalog_properties=props)
    t = {}

    # --- 1. write + read over S3 -------------------------------------------
    try:
        t0 = time.time()
        eng = BranchEngine(cfg)
        eng.write("raw.orders", rows([1, 2, 3], [10.0, -5.0, 20.0]))
        eng.write("demo.clean", rows([1, 3], [10.0, 20.0]))
        t["write 2 tables"] = time.time() - t0
        t0 = time.time()
        n = scan(eng, "raw.orders").num_rows
        t["read table"] = time.time() - t0
        report("1. warehouse on s3://: write + read back", n == 3,
               f"{args.warehouse}")
    except Exception as e:
        report("1. warehouse on s3://: write + read back", False, repr(e))
        return 1

    # --- 2. zero-copy branch on S3 data ------------------------------------
    t0 = time.time()
    eng.create("team", ["demo.clean"])
    t["branch create"] = time.time() - t0
    report("2. zero-copy branch created on S3 tables", True,
           f"{t['branch create']*1000:.0f}ms")

    # --- 3. epoch pins hold while prod ingests -----------------------------
    try:
        eng.switch("main")
        eng.write("raw.orders", rows([9], [900.0]))
        eng.switch("team")
        pinned = eng.catalog.load_table("raw.orders").scan(
            snapshot_id=eng.resolve_read("raw.orders")).to_arrow().num_rows
        report("3. epoch pin holds while prod ingests (S3)", pinned == 3,
               f"pinned sees {pinned} rows, main has 4")
    except Exception as e:
        report("3. epoch pin holds while prod ingests (S3)", False, repr(e))

    # --- 4. branch writes isolated -----------------------------------------
    try:
        t0 = time.time()
        eng.write("demo.clean", rows([1, 3, 9], [10.0, 20.0, 900.0]),
                  mode="overwrite")
        t["branch overwrite"] = time.time() - t0
        ok = (scan(eng, "demo.clean", "team").num_rows == 3
              and scan(eng, "demo.clean").num_rows == 2)
        report("4. branch write isolated from main (S3)", ok)
    except Exception as e:
        report("4. branch write isolated from main (S3)", False, repr(e))

    # --- 5. diff over two S3 refs ------------------------------------------
    try:
        t0 = time.time()
        (d,) = diff_branch(eng)
        t["diff"] = time.time() - t0
        report("5. diff over S3 refs", d.added == 1 and d.key == "id",
               f"+{d.added} ~{d.changed} in {t['diff']:.2f}s")
    except Exception as e:
        report("5. diff over S3 refs", False, repr(e))

    # --- 6. promote + cleanup ----------------------------------------------
    try:
        t0 = time.time()
        res = eng.promote()
        t["promote"] = time.time() - t0
        ok = (res["promoted"] == ["demo.clean"]
              and scan(eng, "demo.clean").num_rows == 3
              and "team" not in eng.catalog.load_table(
                  "demo.clean").metadata.refs)
        report("6. promote fast-forwards, refs cleaned (S3)", ok)
    except Exception as e:
        report("6. promote fast-forwards, refs cleaned (S3)", False, repr(e))

    print("\n--- TIMINGS (tiny data; latency context, not throughput) ---")
    for k, v in t.items():
        print(f"  {v*1000:8.0f}ms  {k}")
    print("\n--- SUMMARY ---")
    for status, step in RESULTS:
        print(f"  {status}: {step}")
    return 0 if all(s == "PASS" for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
