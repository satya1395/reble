"""Provenance: reble.* keys ride snapshot summaries; SQL bundle in manifests."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from reble.catalog import get_head, get_ref_snapshot
from reble.cli import app

runner = CliRunner()


def _summary(catalog, table_id: str, snapshot_id: int) -> dict:
    snapshot = catalog.load_table(table_id).snapshot_by_id(snapshot_id)
    return dict(snapshot.summary.additional_properties or {})


def test_branch_snapshots_carry_provenance(project, seeded_catalog):
    result = runner.invoke(app, ["--json", "run"])
    assert result.exit_code == 0, result.stdout
    env = json.loads(result.stdout)
    run_id = env["data"]["run_id"]

    mart_hash = next(
        r["ast_hash"] for r in env["data"]["results"] if r["model"] == "mart_orders"
    )
    snap = get_ref_snapshot(
        seeded_catalog.load_table("analytics.mart_orders"), "fix_orders"
    )
    summary = _summary(seeded_catalog, "analytics.mart_orders", snap)
    assert summary["reble.model"] == "mart_orders"
    assert summary["reble.ast_hash"] == mart_hash
    assert summary["reble.run_id"] == run_id
    assert summary["reble.changeset"] == "fix-orders"
    assert "reble.seed" not in summary


def test_seed_snapshots_are_marked(project, seeded_catalog):
    runner.invoke(app, ["run"])
    # stg_orders was a new model: its first main snapshot is the zero-row seed
    stg = seeded_catalog.load_table("analytics.stg_orders")
    first_snapshot = min(
        stg.metadata.snapshots,
        key=lambda s: s.sequence_number if s.sequence_number is not None else 0,
    )
    seed_summary = _summary(seeded_catalog, "analytics.stg_orders", first_snapshot.snapshot_id)
    assert seed_summary.get("reble.seed") == "true"
    assert seed_summary.get("reble.model") == "stg_orders"
    # the seed carries no rows
    assert stg.scan(snapshot_id=first_snapshot.snapshot_id).to_arrow().num_rows == 0


def test_provenance_survives_promote_to_main(project, seeded_catalog):
    runner.invoke(app, ["run"])
    branch_snap = get_ref_snapshot(
        seeded_catalog.load_table("analytics.mart_orders"), "fix_orders"
    )
    runner.invoke(app, ["promote"])

    main_snap = get_head(seeded_catalog, "analytics.mart_orders", "main")
    assert main_snap == branch_snap  # promote re-points main; same snapshot
    summary = _summary(seeded_catalog, "analytics.mart_orders", main_snap)
    assert summary["reble.model"] == "mart_orders"
    assert summary["reble.run_id"]


def test_run_manifest_carries_sql_bundle(project, seeded_catalog):
    result = runner.invoke(app, ["--json", "run"])
    env = json.loads(result.stdout)
    stg = next(r for r in env["data"]["results"] if r["model"] == "stg_orders")
    assert "raw_events" in stg["sql"]
    from reble.state import StateStore
    store = StateStore(store="local", reble_dir=project / ".reble")
    hashes = store.load_run_hashes(env["data"]["run_id"])
    assert hashes  # manifest is queryable from the state store
