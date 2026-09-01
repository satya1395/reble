"""Spike 08 — off-the-shelf clients through `reble serve` (see PLAN.md).

Exit criteria: pyiceberg REST client AND DuckDB `ATTACH (TYPE iceberg,
ENDPOINT …)` both read the BRANCH's rows; the pinned table shows pinned
rows while main has newer data; write attempts get 405.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

WORK = Path(__file__).parent / "work"
PORT = 8199


def cli(*args, cwd):
    r = subprocess.run([sys.executable, "-m", "reble.cli", *args],
                       cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, (args, r.stdout, r.stderr)
    return r.stdout


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    cli("init", "proj", cwd=WORK)
    proj = WORK / "proj"

    (proj / "seeds").mkdir()
    (proj / "seeds/orders.csv").write_text(
        "order_id,amount\n1,10\n2,20\n3,30\n")
    cli("load", "raw.orders", "seeds/orders.csv", cwd=proj)
    cli("run", cwd=proj)
    cli("branch", "create", "b1", "--tables", "demo.example", cwd=proj)
    (proj / "models/demo/example.sql").write_text(
        "SELECT 1 AS id, 'branch-view' AS message")
    cli("run", cwd=proj)
    cli("branch", "switch", "main", cwd=proj)
    (proj / "seeds/more.csv").write_text("order_id,amount\n4,40\n5,50\n")
    cli("load", "raw.orders", "seeds/more.csv", cwd=proj)
    cli("branch", "switch", "b1", cwd=proj)
    print("staged: branch b1 (demo.example edited), raw.orders pinned at 3, "
          "main at 5")

    from reble.config import load_config
    from reble.serve import make_server
    holder = {}

    def run_server():
        holder["srv"] = make_server(load_config(proj), port=PORT)
        holder["ready"] = True
        holder["srv"].serve_forever()

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    while not holder.get("ready"):
        time.sleep(0.05)
    uri = f"http://127.0.0.1:{PORT}"
    print(f"serving at {uri}")

    # -- client 1: pyiceberg REST ---------------------------------------------
    from pyiceberg.catalog import load_catalog
    cat = load_catalog("proxy", type="rest", uri=uri)
    msg = cat.load_table("demo.example").scan().to_arrow()["message"].to_pylist()
    assert msg == ["branch-view"], msg
    pinned = cat.load_table("raw.orders").scan().to_arrow().num_rows
    assert pinned == 3, pinned
    print("pyiceberg REST client: branch rows ✓, pinned rows ✓")

    # -- client 2: DuckDB ATTACH ----------------------------------------------
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL iceberg; LOAD iceberg;")
    attached = None
    for attach_sql in [
        f"ATTACH 'warehouse' AS wh (TYPE iceberg, ENDPOINT '{uri}', "
        f"AUTHORIZATION_TYPE 'none')",
        f"ATTACH '' AS wh (TYPE iceberg, ENDPOINT '{uri}', "
        f"AUTHORIZATION_TYPE 'none')",
        f"ATTACH 'warehouse' AS wh (TYPE iceberg, ENDPOINT '{uri}')",
    ]:
        try:
            con.execute(attach_sql)
            attached = attach_sql
            break
        except Exception as e:
            print(f"  attach variant failed: {e}\n    ({attach_sql})")
    assert attached, "no ATTACH variant worked"
    print(f"duckdb attach OK via: {attached}")
    msg = [r[0] for r in
           con.execute("SELECT message FROM wh.demo.example").fetchall()]
    assert msg == ["branch-view"], msg
    pinned = con.execute("SELECT count(*) FROM wh.raw.orders").fetchone()[0]
    assert pinned == 3, pinned
    print(f"duckdb {duckdb.__version__}: branch rows ✓, pinned rows ✓ "
          "(main has 5)")

    # -- writes refused --------------------------------------------------------
    import requests
    r = requests.post(f"{uri}/v1/namespaces", json={}, timeout=5)
    assert r.status_code == 405
    print("write attempt: 405 ✓")

    holder["srv"].shutdown()
    print("\nSPIKE 08: PASS — stock pyiceberg and stock DuckDB both see the "
          "branch's world through reble serve")


if __name__ == "__main__":
    main()
