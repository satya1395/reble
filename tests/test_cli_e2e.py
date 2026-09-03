"""End-to-end CLI lifecycle: run → diff → status → promote (+ exit codes)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from reble import __version__
from reble.cli import app

runner = CliRunner()


def _invoke(*args, expect=0):
    result = runner.invoke(app, [*args])
    assert result.exit_code == expect, (
        f"exit {result.exit_code} != {expect}\nstdout: {result.stdout}\n"
        f"exception: {result.exception}"
    )
    return result


def _json(*args, expect=0) -> dict:
    result = _invoke("--json", *args, expect=expect)
    env = json.loads(result.stdout)
    # envelope shape is normative (spec §6)
    assert set(env) == {"reble", "command", "ok", "branch", "data", "warnings", "errors"}
    assert env["reble"] == __version__
    return env


def test_version():
    result = _invoke("--version")
    assert __version__ in result.stdout


# ---------------------------------------------------------------- init

def test_init_writes_config_and_gitignore(tmp_path):
    (tmp_path / "models").mkdir()
    result = runner.invoke(
        app,
        [
            "init", "--catalog", "sql", "--namespace", "analytics",
            "--config", str(tmp_path / "reble.yml"),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "reble.yml").exists()
    assert ".reble/" in (tmp_path / ".gitignore").read_text()
    assert (tmp_path / ".reble").is_dir()
    # detected vs assumed are labeled
    assert "detected:" in result.stdout


# ---------------------------------------------------------------- run

def test_run_scopes_pins_and_writes_on_branch(project, seeded_catalog):
    _invoke("run")

    # branch refs exist on all three scope tables, main has no model data yet
    for t in ("stg_orders", "mart_orders", "report_daily"):
        refs = seeded_catalog.load_table(f"analytics.{t}").refs()
        assert "fix_orders" in refs
        assert "main" in refs
    assert seeded_catalog.load_table("analytics.stg_orders").scan().to_arrow().num_rows == 0

    # pin tag blocks the input
    pins = seeded_catalog.load_table("analytics.raw_events").refs()
    assert "reble_pin__fix_orders__raw_events" in pins

    # branch reads see the model outputs
    from reble.catalog import get_ref_snapshot

    stg = seeded_catalog.load_table("analytics.stg_orders")
    branch_snap = get_ref_snapshot(stg, "fix_orders")
    assert stg.scan(snapshot_id=branch_snap).to_arrow().num_rows == 3


def test_run_dry_run_writes_nothing(project, seeded_catalog):
    result = _invoke("run", "--dry-run")
    assert "scope (edited)" in result.stdout
    assert "reble_pin__fix_orders__raw_events" in result.stdout
    # nothing materialized: no model tables, no pin tags
    assert seeded_catalog.list_tables("analytics") == [("analytics", "raw_events")]
    assert "reble_pin__fix_orders__raw_events" not in seeded_catalog.load_table(
        "analytics.raw_events"
    ).refs()


def test_run_is_idempotent_per_model(project, seeded_catalog):
    first = _json("run")
    statuses = {r["model"]: r["status"] for r in first["data"]["results"]}
    assert set(statuses.values()) == {"ran"}

    second = _json("run")
    statuses2 = {r["model"]: r["status"] for r in second["data"]["results"]}
    assert set(statuses2.values()) == {"skipped"}


def test_cosmetic_edit_does_not_trigger_rerun(project, seeded_catalog):
    _invoke("run")
    (project / "models" / "stg_orders.sql").write_text(
        "-- kind: table\nSELECT * FROM raw_events WHERE amount > 5\n"  # case + caps
    )
    env = _json("run")
    statuses = {r["model"]: r["status"] for r in env["data"]["results"]}
    assert statuses == {"stg_orders": "skipped", "mart_orders": "skipped", "report_daily": "skipped"}


def test_depth_cap(project, seeded_catalog):
    env = _json("run", "--depth", "1", "--models", "stg_orders")
    scope = env["data"]["scope"]
    # report_daily is 2 hops downstream → cut off and marked stale
    assert scope["edited"] == ["stg_orders"]
    assert scope["downstream"] == ["mart_orders"]
    assert scope["stale_by_depth"] == ["report_daily"]


def test_new_branch_with_no_edits_is_legal(project, seeded_catalog):
    # restore the committed model content → no edits vs branch point
    import subprocess

    subprocess.run(["git", "checkout", "--", "models"], cwd=project, check=True, capture_output=True)
    env = _json("run")
    assert env["data"]["status"].startswith("empty scope")
    assert env["ok"] is True


# ---------------------------------------------------------------- diff

def test_diff_keyed_counts_and_envelope(project, seeded_catalog):
    _invoke("run")
    env = _json("diff", "mart_orders")
    table = env["data"]["tables"][0]
    assert table["table"] == "analytics.mart_orders"
    assert table["key_columns"] == ["order_id"]
    assert table["added"] == 3
    assert table["removed"] == 0
    assert table["samples"]["added"][0]["amount_doubled"] == 20.0


def test_diff_hash_mode_warning(project, seeded_catalog):
    _invoke("run")
    env = _json("diff", "stg_orders")  # stg_orders has no key: header
    table = env["data"]["tables"][0]
    assert table["key_columns"] is None
    assert table["warning"] and "full-hash compare" in table["warning"]


def test_diff_missing_key_error_is_exit_7(project, seeded_catalog):
    (project / "reble.yml").write_text(
        (project / "reble.yml").read_text().replace(
            "lineage:", "diff:\n  on_missing_key: error\nlineage:"
        )
    )
    _invoke("run")
    _invoke("diff", "stg_orders", expect=7)


# ---------------------------------------------------------------- status

def test_status_clean_then_drift_exit_3(project, seeded_catalog):
    _invoke("run")
    _invoke("status")  # clean → 0

    import pyarrow as pa

    seeded_catalog.load_table("analytics.raw_events").append(
        pa.table({"order_id": [4], "amount": [40.0]})
    )
    env = _json("status", expect=3)
    assert env["ok"] is False
    assert env["data"]["data"]["drifted"]


# ---------------------------------------------------------------- promote

def test_promote_clean_fast_forward(project, seeded_catalog):
    _invoke("run")
    result = _invoke("promote")
    for t in ("stg_orders", "mart_orders", "report_daily"):
        assert seeded_catalog.load_table(f"analytics.{t}").scan().to_arrow().num_rows == 3
    assert "no merge, ever" in result.stdout
    _invoke("status")  # clean after promote


def test_promote_ff_only_blocked_on_drift(project, seeded_catalog):
    import pyarrow as pa

    _invoke("run")
    seeded_catalog.load_table("analytics.raw_events").append(
        pa.table({"order_id": [4], "amount": [40.0]})
    )
    _invoke("promote", "--ff-only", expect=4)


def test_promote_with_drift_reruns_and_promotes(project, seeded_catalog):
    import pyarrow as pa

    _invoke("run")
    seeded_catalog.load_table("analytics.raw_events").append(
        pa.table({"order_id": [4], "amount": [40.0]})
    )
    result = _invoke("promote")
    # drift announce is an envelope warning → stderr in text mode
    assert "drift on" in (result.stdout + getattr(result, "stderr", ""))
    # the new row flowed through the re-run into main
    rows = seeded_catalog.load_table("analytics.mart_orders").scan().to_arrow().to_pylist()
    assert {r["order_id"] for r in rows} == {1, 2, 3, 4}
    assert {r["amount_doubled"] for r in rows} == {20.0, 40.0, 60.0, 80.0}


# ---------------------------------------------------------------- branch / gc

def test_branch_show_and_discard(project, seeded_catalog):
    _invoke("run")
    env = _json("branch", "show", "fix_orders")
    assert any(r["ref"] == "fix_orders" for r in env["data"]["refs"])
    assert any(r["ref"].startswith("reble_pin__fix_orders__") for r in env["data"]["refs"])

    _invoke("branch", "discard", "fix_orders", "--yes")
    refs = seeded_catalog.load_table("analytics.stg_orders").refs()
    assert "fix_orders" not in refs
    pins = seeded_catalog.load_table("analytics.raw_events").refs()
    assert not [r for r in pins if r.startswith("reble_pin__fix_orders__")]


def test_gc_drops_orphan_pin_tags(project, seeded_catalog):
    _invoke("run")
    _invoke("branch", "discard", "fix_orders", "--yes")
    # discard drops that branch's pins; fabricate a true orphan
    snap = seeded_catalog.load_table("analytics.raw_events").metadata.current_snapshot_id
    seeded_catalog.load_table("analytics.raw_events").manage_snapshots().create_tag(
        snapshot_id=snap, tag_name="reble_pin__ghost__raw_events"
    ).commit()

    env = _json("gc", "--dry-run")
    assert any("reble_pin__ghost__raw_events" in t for t in env["data"]["orphan_tags"])

    _invoke("gc")
    refs = seeded_catalog.load_table("analytics.raw_events").refs()
    assert "reble_pin__ghost__raw_events" not in refs


def test_discard_refuses_during_promote(project, seeded_catalog):
    _invoke("run")
    from reble.state import StateStore

    _store = StateStore(store="local", reble_dir=project / ".reble")
    _store.validate()
    _store.save_promote_record({"branch": "fix_orders", "tables": {}})
    _invoke("branch", "discard", "fix_orders", "--yes", expect=1)


def test_estimate_reports_scope_and_inputs(project, seeded_catalog):
    runner.invoke(app, ["run"])
    result = runner.invoke(app, ["--json", "estimate"])
    assert result.exit_code == 0, result.stdout
    env = json.loads(result.stdout)
    data = env["data"]
    assert data["models"] == 3
    roles = {t["table"]: t["role"] for t in data["tables"]}
    assert roles["analytics.raw_events"] == "input"
    assert roles["analytics.stg_orders"] == "scope"
    assert data["est_bytes_read"] > 0
    assert any("rough" in w for w in env["warnings"])
    # per-table numbers come from snapshot summaries (rows > 0 for materialized tables)
    stg = next(t for t in data["tables"] if t["table"].endswith("stg_orders"))
    assert stg["records"] == 3


def test_estimate_empty_scope(project, seeded_catalog):
    import subprocess

    subprocess.run(["git", "checkout", "--", "models"], cwd=project, check=True, capture_output=True)
    result = runner.invoke(app, ["--json", "estimate"])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["data"]["status"] == "empty scope"


def test_diff_text_stats_and_saved_files(project, seeded_catalog):
    """`reble diff` prints row stats only; sample rows are saved as per-table
    JSON under .reble/diffs/<change-set>/, and --rows caps what is saved."""
    import json

    from typer.testing import CliRunner

    from reble.cli import app

    _invoke("run")
    result = CliRunner().invoke(app, ["diff", "mart_orders"])
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert any("analytics.mart_orders: +3" in ln for ln in lines)
    assert not any(ln.lstrip().startswith(("+ ", "- ", "~ ")) for ln in lines)
    assert any(ln.startswith("detail: ") for ln in lines)

    diff_dir = project / ".reble" / "diffs" / "fix-orders"
    saved = json.loads((diff_dir / "analytics.mart_orders.json").read_text())
    assert saved["added"] == 3
    assert saved["samples"]["added"]  # default cap saved the detail
    summary = json.loads((diff_dir / "summary.json").read_text())
    assert "analytics.mart_orders" in summary

    CliRunner().invoke(app, ["diff", "mart_orders", "--rows", "2"])
    saved = json.loads((diff_dir / "analytics.mart_orders.json").read_text())
    assert len(saved["samples"]["added"]) == 2
