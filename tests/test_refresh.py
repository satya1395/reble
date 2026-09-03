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
