"""--refresh: data-driven scope for the nightly-refresh case."""

from __future__ import annotations

import json
import subprocess

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from reble.cli import app

runner = CliRunner()


def _append_raw(seeded_catalog, rows: dict) -> None:
    seeded_catalog.load_table("analytics.raw_events").append(pa.table(rows))


def test_refresh_picks_up_new_upstream_data(project, seeded_catalog):
    # build everything once (first run declares its scope)…
    result = runner.invoke(app, ["run", "--models", "stg_orders,mart_orders,report_daily"])
    assert result.exit_code == 0, result.stdout
    # …on a committed tree, like a nightly cron would have
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "build"],
        cwd=project, check=True, capture_output=True,
    )
    # the nightly-refresh case is a clean tree on main
    subprocess.run(["git", "checkout", "-q", "main"], cwd=project, check=True, capture_output=True)

    # new data lands on main; SQL unchanged → plain run sees empty scope
    _append_raw(seeded_catalog, {"order_id": [4], "amount": [40.0]})
    plain = json.loads(runner.invoke(app, ["--json", "run"]).stdout)
    assert plain["data"]["status"].startswith("empty scope")

    # --refresh sees it: stg is stale (its input moved) and the closure
    # pulls in mart + report
    refreshed = json.loads(runner.invoke(app, ["--json", "run", "--refresh"]).stdout)
    statuses = {r["model"]: r["status"] for r in refreshed["data"]["results"]}
    assert set(statuses.values()) == {"ran"}
    assert set(statuses) == {"stg_orders", "mart_orders", "report_daily"}

    from reble.catalog import get_head

    mart = seeded_catalog.load_table("analytics.mart_orders")
    rows = mart.scan(snapshot_id=get_head(seeded_catalog, "analytics.mart_orders", "main")).to_arrow().to_pylist()
    # main's committed SQL filters amount > 15: the refresh rebuilt main's
    # models over the new data — orders 2,3 (old) and 4 (new) survive
    assert {r["order_id"] for r in rows} == {2, 3, 4}


def test_refresh_noop_when_nothing_moved(project, seeded_catalog):
    runner.invoke(app, ["run", "--models", "stg_orders,mart_orders,report_daily"])
    again = json.loads(runner.invoke(app, ["--json", "run", "--refresh"]).stdout)
    assert again["data"]["status"].startswith("empty scope")


def test_refresh_builds_unmaterialized_models(project, seeded_catalog):
    result = json.loads(runner.invoke(app, ["--json", "run", "--refresh"]).stdout)
    statuses = {r["model"]: r["status"] for r in result["data"]["results"]}
    assert set(statuses) == {"stg_orders", "mart_orders", "report_daily"}
    assert set(statuses.values()) == {"ran"}


def test_refresh_rejects_models_combo(project, seeded_catalog):
    result = runner.invoke(app, ["run", "--refresh", "--models", "stg_orders"])
    assert result.exit_code == 2


def test_estimate_refresh_scope(project, seeded_catalog):
    runner.invoke(app, ["run", "--models", "stg_orders,mart_orders,report_daily"])
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "build"],
        cwd=project, check=True, capture_output=True,
    )
    subprocess.run(["git", "checkout", "-q", "main"], cwd=project, check=True, capture_output=True)
    _append_raw(seeded_catalog, {"order_id": [4], "amount": [40.0]})
    env = json.loads(runner.invoke(app, ["--json", "estimate", "--refresh"]).stdout)
    assert env["data"]["models"] == 3  # stg stale + downstream closure
    before = json.loads(runner.invoke(app, ["--json", "estimate"]).stdout)
    assert before["data"]["status"].startswith("empty scope")


@pytest.mark.parametrize("flag", ["--refresh"])
def test_run_refresh_dry_run_writes_nothing(project, seeded_catalog, flag):
    result = runner.invoke(app, ["run", flag, "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "scope (edited)" in result.stdout
    assert seeded_catalog.list_tables("analytics") == [("analytics", "raw_events")]


def test_force_rebuilds_unchanged_models(project, seeded_catalog):
    """Answer to 'can you force a full rebuild': --models alone skips on
    unchanged hashes; --force rebuilds. Reruns are replace-not-append —
    no duplication."""
    runner.invoke(app, ["run", "--models", "stg_orders,mart_orders"])

    # plain --models rerun: everything skips (hashes match)
    again = json.loads(
        runner.invoke(app, ["--json", "run", "--models", "stg_orders,mart_orders"]).stdout
    )
    statuses = {r["model"]: r["status"] for r in again["data"]["results"]}
    assert set(statuses.values()) == {"skipped"}

    # --force: everything reruns, replace-not-append
    forced = json.loads(
        runner.invoke(app, ["--json", "run", "--models", "stg_orders,mart_orders", "--force"]).stdout
    )
    statuses = {r["model"]: r["status"] for r in forced["data"]["results"]}
    assert set(statuses.values()) == {"ran"}

    from reble.catalog import get_ref_snapshot

    mart = seeded_catalog.load_table("analytics.mart_orders")
    branch_snap = get_ref_snapshot(mart, "fix_orders")
    rows = mart.scan(snapshot_id=branch_snap).to_arrow().to_pylist()
    # exactly one materialization's worth of rows — the forced rerun
    # replaced, it did not append (working-tree SQL is amount > 5: all 3)
    assert len(rows) == 3


def test_crashed_run_resumes_without_rerunning_completed(project, seeded_catalog, monkeypatch):
    """The Airflow-retry question, answered by test: a mid-run failure leaves
    completed models recorded; the retry runs only what didn't finish."""
    from reble.engine import DuckDbEngine

    original = DuckDbEngine.execute_model

    def fail_on_mart(self, model, *args, **kwargs):
        if model.name == "mart_orders":
            raise RuntimeError("simulated mid-run failure")
        return original(self, model, *args, **kwargs)

    monkeypatch.setattr(DuckDbEngine, "execute_model", fail_on_mart)
    result = runner.invoke(app, ["run", "--models", "stg_orders,mart_orders,report_daily"])
    assert result.exit_code == 1, result.stdout  # run failed at mart_orders

    # stg_orders completed and its progress was persisted despite the crash
    import json as _json

    from reble.state import StateStore
    store = StateStore(store="local", reble_dir=project / ".reble")
    branch = store.load().branches  # dict keyed by changeset
    any_stg = any("stg_orders" in bs.model_hashes for bs in branch.values())
    assert any_stg, "stg_orders hash should be persisted despite crash"

    # retry: stg skips (already done), mart + downstream run
    monkeypatch.setattr(DuckDbEngine, "execute_model", original)
    retried = _json.loads(
        runner.invoke(app, ["--json", "run", "--models", "stg_orders,mart_orders,report_daily"]).stdout
    )
    statuses = {r["model"]: r["status"] for r in retried["data"]["results"]}
    assert statuses == {
        "stg_orders": "skipped",
        "mart_orders": "ran",
        "report_daily": "ran",
    }


def test_force_alone_rebuilds_everything(project, seeded_catalog):
    """`reble run --force` with no edits and no --models is a full rebuild —
    not "empty scope". The engine-switch case: nothing changed, everything
    must run through the new engine anyway."""
    runner.invoke(app, ["run"])  # first run: stg_orders + mart_orders

    forced = json.loads(runner.invoke(app, ["--json", "run", "--force"]).stdout)
    assert forced["ok"] is True
    statuses = {r["model"]: r["status"] for r in forced["data"]["results"]}
    assert set(statuses) == {"stg_orders", "mart_orders", "report_daily"}
    assert set(statuses.values()) == {"ran"}
