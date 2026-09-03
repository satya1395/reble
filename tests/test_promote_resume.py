"""Promote re-entrancy: partial failure resumes without re-running scope."""

from __future__ import annotations

from typer.testing import CliRunner

from reble import catalog as ice
from reble.cli import app

runner = CliRunner()


def test_partial_failure_resumes_without_rerun(project, seeded_catalog, monkeypatch):
    runner.invoke(app, ["run"])

    # Fail the fast-forward for exactly one table, once.
    original_ff = ice.fast_forward
    failed = {"done": False}

    def flaky(catalog, table_id, branch, snapshot_id):
        if not failed["done"] and table_id.endswith("mart_orders"):
            failed["done"] = True
            raise RuntimeError("simulated commit failure")
        return original_ff(catalog, table_id, branch, snapshot_id)

    monkeypatch.setattr("reble.promote.ice.fast_forward", flaky)

    result = runner.invoke(app, ["promote"])
    assert result.exit_code == 0, result.stdout
    assert "analytics.mart_orders: failed" in result.stdout
    assert "analytics.stg_orders: promoted" in result.stdout
    # promote record retained for resume
    from reble.state import StateStore
    _store = StateStore(store="local", reble_dir=project / ".reble")
    assert _store.load_promote_record("fix_orders") is not None

    from reble.state import StateStore
    _store = StateStore(store="local", reble_dir=project / ".reble")
    _runs_before = len(_store.load().branches)  # snapshot for comparison

    monkeypatch.setattr("reble.promote.ice.fast_forward", original_ff)
    resumed = runner.invoke(app, ["promote"])
    assert resumed.exit_code == 0, resumed.stdout
    assert "analytics.mart_orders: promoted" in resumed.stdout
    assert "analytics.stg_orders: promoted (resumed)" in resumed.stdout

    # resume must NOT have re-run scope: no new run manifests were created
    _runs_after = len(_store.load().branches)
    assert _runs_after == _runs_before  # no new state branches created by resume

    # record cleaned up; everything on main; status clean
    assert _store.load_promote_record("fix_orders") is None
    for t in ("stg_orders", "mart_orders", "report_daily"):
        rows = seeded_catalog.load_table(f"analytics.{t}").scan().to_arrow().num_rows
        assert rows == 3
    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.stdout


def test_partial_failure_state_survives_reload(project, seeded_catalog, monkeypatch):
    """Base-head advancement is persisted per table, not only in memory."""
    runner.invoke(app, ["run"])

    original_ff = ice.fast_forward

    def fail_mart(catalog, table_id, branch, snapshot_id):
        if table_id.endswith("mart_orders"):
            raise RuntimeError("simulated commit failure")
        return original_ff(catalog, table_id, branch, snapshot_id)

    monkeypatch.setattr("reble.promote.ice.fast_forward", fail_mart)
    runner.invoke(app, ["promote"])

    # reload state from disk exactly as a new process would
    from reble.promote import Promoter
    from reble.state import StateStore

    st = StateStore(store="local", reble_dir=project / ".reble").load().branches["fix-orders"]
    stg_head = ice.get_head(seeded_catalog, "analytics.stg_orders", "main")
    assert st.base_heads["analytics.stg_orders"] == stg_head

    # a fresh preflight sees no drift for the promoted table
    from reble.state import StateStore
    _s2 = StateStore(store="local", reble_dir=project / ".reble")
    reports = Promoter(None, seeded_catalog, load_record=_s2.load_promote_record).preflight(st)
    drifted = {r.table for r in reports if r.drifted}
    assert "analytics.stg_orders" not in drifted
