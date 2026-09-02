"""Iceberg branch/pin/fast-forward ops on a real (SQL) catalog."""

from __future__ import annotations

import pyarrow as pa
import pytest
from conftest import main_head

from reble import catalog as ice


def _append(catalog, table_id, rows):
    table = catalog.load_table(table_id)
    table.append(pa.table(rows))


@pytest.fixture()
def tracked(seeded_catalog):
    """raw_events with two main snapshots."""
    _append(seeded_catalog, "analytics.raw_events", {"order_id": [9], "amount": [1.0]})
    return seeded_catalog


def test_ensure_branch_is_zero_copy_and_idempotent(tracked):
    before = main_head(tracked, "analytics.raw_events")
    head = ice.ensure_branch(tracked, "analytics.raw_events", "b1", "main")
    assert head == before
    again = ice.ensure_branch(tracked, "analytics.raw_events", "b1", "main")
    assert again == head
    table = tracked.load_table("analytics.raw_events")
    assert "b1" in table.refs()
    # main untouched
    assert main_head(tracked, "analytics.raw_events") == before


def test_pin_snapshot_blocks_epoch_and_retargets(tracked):
    first = main_head(tracked, "analytics.raw_events")
    ice.pin_snapshot(tracked, "analytics.raw_events", "tag1", first)
    assert ice.get_head(tracked, "analytics.raw_events", "tag1") == first

    _append(tracked, "analytics.raw_events", {"order_id": [10], "amount": [2.0]})
    second = main_head(tracked, "analytics.raw_events")
    ice.pin_snapshot(tracked, "analytics.raw_events", "tag1", second)
    assert ice.get_head(tracked, "analytics.raw_events", "tag1") == second


def test_is_fast_forward_by_lineage(tracked):
    ice.ensure_branch(tracked, "analytics.raw_events", "b1", "main")

    # write on the branch (via overwrite) → main is an ancestor
    tracked.load_table("analytics.raw_events").overwrite(
        pa.table({"order_id": [1, 2, 3, 9], "amount": [10.0, 20.0, 30.0, 1.0]}),
        branch="b1",
    )
    table = tracked.load_table("analytics.raw_events")
    assert ice.is_fast_forward(table, "main", "b1")

    # move main forward independently → divergence
    _append(tracked, "analytics.raw_events", {"order_id": [11], "amount": [3.0]})
    table = tracked.load_table("analytics.raw_events")
    assert not ice.is_fast_forward(table, "main", "b1")
    assert ice.is_fast_forward(table, "main", "main")  # equal refs are trivially FF


def test_fast_forward_moves_main(tracked):
    ice.ensure_branch(tracked, "analytics.raw_events", "b1", "main")
    tracked.load_table("analytics.raw_events").overwrite(
        pa.table({"order_id": [1], "amount": [10.0]}), branch="b1"
    )
    branch_head = ice.get_head(tracked, "analytics.raw_events", "b1")
    ice.fast_forward(tracked, "analytics.raw_events", "main", branch_head)
    assert main_head(tracked, "analytics.raw_events") == branch_head
    assert tracked.load_table("analytics.raw_events").scan().to_arrow().num_rows == 1


def test_drop_ref(tracked):
    ice.ensure_branch(tracked, "analytics.raw_events", "b1", "main")
    ice.drop_ref(tracked, "analytics.raw_events", "b1", "branch")
    assert "b1" not in tracked.load_table("analytics.raw_events").refs()
