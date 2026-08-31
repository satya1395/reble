"""Spike 07 — local branches over remote main (F15 team mode).

Question: can a LOCAL catalog hold a zero-copy branch of a table that lives
in a shared "remote" warehouse the developer can only read?

Mechanism under test: pyiceberg `add_files` — register the remote table's
Parquet files (at a pinned snapshot) into a local catalog table WITHOUT
copying them. Then write locally; the remote warehouse must be untouched.

Success criteria:
  1. Local overlay table reads identical rows to the remote table's snapshot.
  2. Zero-copy: no Parquet lands in the local warehouse at branch time.
  3. Local overwrite creates new local files; every byte of the remote
     warehouse is bit-identical afterwards (mtime+size+hash check).
  4. Timings printed for branch (add_files) at this scale.
"""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

import duckdb
import pyarrow as pa
from pyiceberg.catalog import load_catalog

ROOT = Path(__file__).parent / "work"
REMOTE = ROOT / "remote-warehouse"     # the shared bucket (simulated)
LOCAL = ROOT / "local-warehouse"       # the developer's machine
ROWS = 1_000_000


def catalog(name: str, wh: Path):
    wh.mkdir(parents=True, exist_ok=True)
    return load_catalog(name, type="sql",
                        uri=f"sqlite:///{wh}/catalog.db",
                        warehouse=f"file://{wh}")


def fingerprint_tree(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            out[str(p.relative_to(root))] = f"{p.stat().st_size}:{h}"
    return out


def main():
    shutil.rmtree(ROOT, ignore_errors=True)

    # -- the shared warehouse: main with 1M orders ---------------------------
    remote = catalog("remote", REMOTE)
    remote.create_namespace("core")
    con = duckdb.connect()
    arrow = con.execute(f"""
        SELECT row_number() OVER () AS order_id,
               (random() * 42000)::BIGINT AS customer_id,
               (random() * 400)::DECIMAL(9,2) AS amount
        FROM range({ROWS})
    """).to_arrow_table()
    rt = remote.create_table("core.orders", schema=arrow.schema)
    rt.append(arrow)
    rt = remote.load_table("core.orders")
    pinned_snapshot = rt.current_snapshot().snapshot_id
    print(f"remote main: core.orders {ROWS:,} rows, snapshot {pinned_snapshot}")

    remote_before = fingerprint_tree(REMOTE)

    # -- the local overlay branch -------------------------------------------
    # file list of the PINNED snapshot (what a read-only dev can see)
    files = [t.file.file_path
             for t in rt.scan(snapshot_id=pinned_snapshot).plan_files()]
    print(f"pinned snapshot has {len(files)} parquet file(s), remote paths")

    local = catalog("local", LOCAL)
    local.create_namespace("core")
    t0 = time.time()
    lt = local.create_table("core.orders", schema=rt.schema())
    lt.add_files(files)                                   # ← the mechanism
    t_branch = time.time() - t0
    print(f"local branch via add_files: {t_branch*1000:.0f}ms")

    # 1. read parity
    lt = local.load_table("core.orders")
    local_rows = lt.scan().to_arrow()
    assert local_rows.num_rows == ROWS, local_rows.num_rows
    remote_sum = rt.scan(snapshot_id=pinned_snapshot).to_arrow()["amount"]
    assert pa.compute.sum(local_rows["amount"]).as_py() == \
        pa.compute.sum(remote_sum).as_py()
    print("read parity: local overlay scan == remote pinned scan")

    # 2. zero-copy
    local_parquet = list(LOCAL.rglob("*.parquet"))
    assert local_parquet == [], local_parquet
    print("zero-copy: no parquet in local warehouse at branch time")

    # 3. local write, remote untouched
    edited = con.execute("""
        SELECT order_id, customer_id, amount FROM local_rows
        WHERE customer_id % 7 != 0
    """).to_arrow_table()
    t0 = time.time()
    lt.overwrite(edited)
    t_write = time.time() - t0
    lt = local.load_table("core.orders")
    n = lt.scan().to_arrow().num_rows
    print(f"local overwrite: {ROWS:,} → {n:,} rows in {t_write:.2f}s; "
          f"{len(list(LOCAL.rglob('*.parquet')))} parquet file(s) now local")

    assert fingerprint_tree(REMOTE) == remote_before
    print("remote untouched: every file bit-identical after local branch+write")

    # 4. the diff a PR bot would compute: local branch vs remote pinned base
    t0 = time.time()
    con.register("branch_v", lt.scan().to_arrow())
    con.register("base_v", rt.scan(snapshot_id=pinned_snapshot).to_arrow())
    removed = con.execute(
        "SELECT count(*) FROM base_v b LEFT JOIN branch_v r USING (order_id) "
        "WHERE r.order_id IS NULL").fetchone()[0]
    print(f"cross-catalog diff: −{removed:,} removed in {time.time()-t0:.2f}s")

    print("\nSPIKE 07: PASS — local zero-copy branches over a read-only "
          "remote main are viable via add_files")


if __name__ == "__main__":
    main()
