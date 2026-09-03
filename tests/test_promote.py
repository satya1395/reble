"""Promote preflight drift signals and re-entrancy records."""

from __future__ import annotations

from pathlib import Path

from reble.promote import Promoter, orphan_pin_tags
from reble.state import BranchState, Pin


def _state(base_heads=None, pins=None):
    return BranchState(
        git_branch="fix",
        data_branch="fix",
        base_ref="main",
        base_heads=base_heads or {},
        pins=pins or {},
    )


class _FakeCatalog:
    def __init__(self, heads):  # table_id -> snapshot id
        self.heads = heads


def _promoter(heads, tmp_path):
    class Cfg:
        class branching:
            tag_prefix = "reble_pin__"

    return Promoter(Cfg, _FakeCatalog(heads), tmp_path)


def test_preflight_detects_pin_and_base_drift(tmp_path):
    from reble import catalog as ice

    orig = ice.get_head
    ice.get_head = lambda cat, table_id, ref: cat.heads.get(table_id)
    try:
        st = _state(
            base_heads={"analytics.mart": 100},
            pins={"raw_events": Pin("analytics.raw_events", "t", 5, 5)},
        )
        promoter = _promoter(
            {"analytics.mart": 100, "analytics.raw_events": 6}, tmp_path
        )
        reports = {r.table: r for r in promoter.preflight(st)}
        assert reports["analytics.mart"].drifted is False
        assert reports["analytics.raw_events"].drifted is True
        assert reports["analytics.raw_events"].kind == "pin"
        assert reports["analytics.mart"].kind == "base"
    finally:
        ice.get_head = orig


def test_promote_record_round_trip(tmp_path):
    from reble.promote import PromoteRecord
    from reble.state import StateStore

    store = StateStore(store="local", reble_dir=tmp_path)
    store.validate()
    record = PromoteRecord(branch="b")
    record.tables["analytics.t"] = {"status": "promoted"}
    store.save_promote_record(record.to_dict())
    loaded = store.load_promote_record("b")
    assert loaded["tables"]["analytics.t"]["status"] == "promoted"
    assert store.load_promote_record("other") is None  # wrong branch → none


def test_orphan_pin_tags(seeded_catalog, tmp_path):
    from reble.config import ConfigLoader

    cfg = ConfigLoader(Path.cwd()).load()
    table_id = "analytics.raw_events"
    snap = seeded_catalog.load_table(table_id).metadata.current_snapshot_id
    seeded_catalog.load_table(table_id).manage_snapshots().create_tag(
        snapshot_id=snap, tag_name="reble_pin__ghost__raw_events"
    ).commit()

    orphans = orphan_pin_tags(seeded_catalog, cfg, active_tags=set())
    assert ((("analytics", "raw_events")), "reble_pin__ghost__raw_events") in orphans

    active = orphan_pin_tags(
        seeded_catalog, cfg, active_tags={"reble_pin__ghost__raw_events"}
    )
    assert active == []
