"""reble serve: branch-resolved Iceberg REST catalog proxy.

The client in these tests is pyiceberg's real REST catalog client — the
same protocol DuckDB/Spark/Trino speak — pointed at a server running in a
background thread. (DuckDB's own ATTACH is exercised in spike 08, not here:
it downloads an extension at runtime, which has no place in CI.)
"""
from __future__ import annotations

import csv
import socket
import threading

import pytest
import requests
from click.testing import CliRunner

from reble.cli import cli


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """A project with: raw.orders (3 rows on main, 5 after the branch pinned
    it), a branch b1 scoped to demo.example with an edit run on it — served
    on a background thread. Yields (uri, runner, proj)."""
    import os
    tmp_path = tmp_path_factory.mktemp("serve")
    runner = CliRunner()
    prev = os.getcwd()
    os.chdir(tmp_path)
    assert runner.invoke(cli, ["init", "proj"]).exit_code == 0
    proj = tmp_path / "proj"
    os.chdir(proj)

    (proj / "seeds").mkdir()
    with open(proj / "seeds/orders.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "amount"])
        w.writerows([[1, 10], [2, 20], [3, 30]])
    assert runner.invoke(
        cli, ["load", "raw.orders", "seeds/orders.csv"]).exit_code == 0
    assert runner.invoke(cli, ["run"]).exit_code == 0        # baseline main

    out = runner.invoke(cli, ["branch", "create", "b1",
                              "--tables", "demo.example"])
    assert out.exit_code == 0, out.output                    # pins raw.orders
    (proj / "models/demo/example.sql").write_text(
        "SELECT 1 AS id, 'branch-view' AS message")
    assert runner.invoke(cli, ["run"]).exit_code == 0        # branch ref

    # main moves under the branch: two more raw rows
    assert runner.invoke(cli, ["branch", "switch", "main"]).exit_code == 0
    with open(proj / "seeds/more.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["order_id", "amount"])
        w.writerows([[4, 40], [5, 50]])
    assert runner.invoke(
        cli, ["load", "raw.orders", "seeds/more.csv"]).exit_code == 0
    assert runner.invoke(cli, ["branch", "switch", "b1"]).exit_code == 0

    port = _free_port()
    ready = threading.Event()
    holder: dict = {}

    def _run():
        from reble.config import load_config
        from reble.serve import make_server
        srv = make_server(load_config(proj), port=port)
        holder["srv"] = srv
        ready.set()
        srv.serve_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    assert ready.wait(10)
    yield f"http://127.0.0.1:{port}", runner, proj
    holder["srv"].shutdown()
    t.join(5)
    os.chdir(prev)


def _rest_catalog(uri):
    from pyiceberg.catalog import load_catalog
    return load_catalog("proxy", type="rest", uri=uri)


def test_scoped_table_reads_branch_rows(served):
    uri, _, _ = served
    cat = _rest_catalog(uri)
    rows = cat.load_table("demo.example").scan().to_arrow()
    assert rows["message"].to_pylist() == ["branch-view"]


def test_pinned_table_reads_pinned_snapshot(served):
    uri, _, _ = served
    cat = _rest_catalog(uri)
    rows = cat.load_table("raw.orders").scan().to_arrow()
    assert rows.num_rows == 3          # pinned — main has 5 by now


def test_listings_and_missing_table(served):
    uri, _, _ = served
    cat = _rest_catalog(uri)
    namespaces = {".".join(ns) for ns in cat.list_namespaces()}
    assert {"demo", "raw"} <= namespaces
    tables = {t[-1] for t in cat.list_tables("demo")}
    assert "example" in tables
    r = requests.get(f"{uri}/v1/namespaces/demo/tables/nope", timeout=5)
    assert r.status_code == 404


def test_writes_are_refused(served):
    uri, _, _ = served
    for method, path in [
        ("post", "/v1/namespaces"),
        ("post", "/v1/namespaces/demo/tables"),
        ("delete", "/v1/namespaces/demo/tables/example"),
    ]:
        r = getattr(requests, method)(uri + path, json={}, timeout=5)
        assert r.status_code == 405, (method, path, r.status_code)
        assert "read-only" in r.json()["error"]["message"]


def test_branch_refs_are_stripped(served):
    uri, _, _ = served
    r = requests.get(f"{uri}/v1/namespaces/demo/tables/example", timeout=5)
    md = r.json()["metadata"]
    assert "b1" not in md.get("refs", {})       # reble bookkeeping is private
    assert md["current-snapshot-id"] == md["refs"]["main"]["snapshot-id"]
    # DuckDB's REST attach resolves "current" from the snapshot-log tail —
    # the synthesis must agree with current-snapshot-id (spike 08 lesson)
    assert md["snapshot-log"][-1]["snapshot-id"] == md["current-snapshot-id"]


def test_on_main_serves_current_main(served):
    uri, runner, _ = served
    assert runner.invoke(cli, ["branch", "switch", "main"]).exit_code == 0
    cat = _rest_catalog(uri)           # resolution is per-request
    rows = cat.load_table("raw.orders").scan().to_arrow()
    assert rows.num_rows == 5
    assert runner.invoke(cli, ["branch", "switch", "b1"]).exit_code == 0
