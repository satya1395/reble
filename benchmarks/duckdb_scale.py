"""DuckDB read-path benchmark: arrow vs iceberg_scan (DECISIONS §19).

Builds a scratch project (sqlite catalog + file warehouse), seeds a 5M-row
upstream table, materializes two models, then measures — per read mode —
an edited re-run and the row-level diff. Branch creation is timed once
(metadata-only). Not part of CI; run manually:

    .venv/bin/python benchmarks/duckdb_scale.py [--rows 5000000]

Reports wall times and peak RSS (ru_maxrss: KiB on Linux, bytes on macOS).
"""

from __future__ import annotations

import argparse
import resource
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
from pyiceberg.catalog import load_catalog

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from reble.core import Reble

ROWS_DEFAULT = 5_000_000


def build_project(root: Path, rows: int) -> Reble:
    shutil.rmtree(root, ignore_errors=True)
    (root / "models").mkdir(parents=True)
    (root / "warehouse").mkdir()
    (root / "models" / "stg_orders.sql").write_text(
        "-- kind: table\n-- key: id\nselect * from raw_events where amount > 10\n"
    )
    (root / "models" / "mart_orders.sql").write_text(
        "-- kind: table\n-- key: id\n"
        "select id, amount, bucket, amount * 2 as amount_doubled from stg_orders\n"
    )
    (root / "reble.yml").write_text(
        f"""
version: 1
warehouse:
  catalog:
    type: sql
    uri: sqlite:///{root}/catalog.db
    warehouse: file://{root}/warehouse
  namespace: analytics
  default_base: main
branching:
  git_sync: false
lineage:
  models_path: models
  dialect: duckdb
engines:
  duckdb:
    read_mode: arrow
    memory_limit: 2GB
    temp_directory: {root}/spill
"""
    )
    cat = load_catalog(
        "reble", type="sql", uri=f"sqlite:///{root}/catalog.db",
        warehouse=f"file://{root}/warehouse",
    )
    cat.create_namespace_if_not_exists("analytics")
    rng = np.random.default_rng(42)
    table = cat.create_table(
        "analytics.raw_events",
        schema=pa.schema([("id", pa.int64()), ("amount", pa.float64()), ("bucket", pa.string())]),
    )
    for chunk_start in range(0, rows, 1_000_000):
        n = min(1_000_000, rows - chunk_start)
        table.append(
            pa.table(
                {
                    "id": np.arange(chunk_start, chunk_start + n, dtype=np.int64),
                    "amount": rng.uniform(0, 100, n),
                    "bucket": rng.choice(["a", "b", "c"], n),
                }
            )
        )
    core = Reble(root)
    timed(
        f"[metadata] branch create on {rows:,}-row table",
        lambda: _ensure_branch(core.catalog, "analytics.raw_events", "bench-branch"),
    )
    return core


def _ensure_branch(catalog, table_id: str, branch: str) -> None:
    from reble import catalog as ice

    ice.ensure_branch(catalog, table_id, branch, "main")


def timed(label: str, fn) -> float:
    start = time.time()
    fn()
    elapsed = time.time() - start
    print(f"  {label:<42} {elapsed:8.2f}s")
    return elapsed


def peak_rss_gb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1e9 if sys.platform == "darwin" else rss / 1e6


def run_mode(mode: str, rows: int, root: Path | None = None) -> None:
    root = root or Path(f"/tmp/reble-bench/{mode}")
    print(f"[{mode}] building fresh project ({rows:,} rows) …")
    core = build_project(root, rows)

    # first materialization (both models on the branch)
    timed(f"[{mode}] first run (materialize 2 models)", lambda: core.run(
        models=["stg_orders", "mart_orders"], change_set=f"bench-{mode}")
    )

    # edited re-run + diff (same edit for every mode)
    stg = root / "models" / "stg_orders.sql"
    stg.write_text(
        "-- kind: table\n-- key: id\nselect * from raw_events where amount > 25\n"
    )
    core = Reble(root)  # fresh state read
    timed(f"[{mode}] edited re-run (scope: both models)", lambda: core.run(
        change_set=f"bench-{mode}")
    )
    env = core.diff(tables=["mart_orders"], change_set=f"bench-{mode}")
    table = env["data"]["tables"][0]
    print(
        f"  [{mode}] diff result: +{table['added']} -{table['removed']} "
        f"~{table['changed']} (keys={table['key_columns']})"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=ROWS_DEFAULT)
    parser.add_argument("--root", type=Path, default=Path("/tmp/reble-bench"))
    args = parser.parse_args()

    for mode in ("arrow", "auto"):
        run_mode(mode, args.rows, root=args.root / mode)
    print(f"  peak RSS (process, incl. seeding):     {peak_rss_gb():.2f} GB")


if __name__ == "__main__":
    main()
