import pyarrow as pa
import pytest

from reble.branches import BranchEngine
from reble.config import RebleConfig
from reble.diffing import diff_branch
from reble.errors import BranchError


def t(ids, amounts):
    return pa.table({"id": pa.array(ids, pa.int64()),
                     "amount": pa.array(amounts, pa.float64())})


@pytest.fixture
def eng(tmp_path):
    (tmp_path / "warehouse").mkdir()
    e = BranchEngine(RebleConfig(project_dir=tmp_path))
    e.write("demo.orders", t([1, 2, 3], [10.0, 20.0, 30.0]))
    return e


def rows(eng, table, snap=None):
    return eng.catalog.load_table(table).scan(snapshot_id=snap).to_arrow()


def test_diff_counts_added_removed_changed(eng):
    eng.create("dev", ["demo.orders"])
    # overwrite on branch: id 1 kept, id 2 changed, id 3 removed, id 4 added
    eng.write("demo.orders", t([1, 2, 4], [10.0, 99.0, 40.0]), mode="overwrite")
    (d,) = diff_branch(eng)
    assert (d.kind, d.key) == ("diff", "id")
    assert (d.added, d.removed, d.changed) == (1, 1, 1)
    assert (d.rows_main, d.rows_branch) == (3, 3)


def test_diff_profile_for_new_table(eng):
    eng.create("feat", ["demo.fresh"])
    eng.write("demo.fresh", t([1, 2], [5.0, None]))
    (d,) = diff_branch(eng)
    assert d.kind == "profile"
    assert d.rows_branch == 2
    assert ("amount", "double", 1) in d.profile_columns


def test_diff_refuses_on_main(eng):
    with pytest.raises(BranchError):
        diff_branch(eng)


def test_promote_fast_forward(eng):
    eng.create("dev", ["demo.orders"])
    eng.write("demo.orders", t([1, 2, 3, 4], [10.0, 20.0, 30.0, 40.0]),
              mode="overwrite")
    res = eng.promote()
    assert res["promoted"] == ["demo.orders"]
    assert eng.state.current_branch() == "main"
    assert rows(eng, "demo.orders").num_rows == 4            # main advanced
    assert "dev" not in eng.catalog.load_table("demo.orders").metadata.refs
    assert eng.state.get("dev") is None


def test_promote_new_table(eng):
    eng.create("feat", ["demo.fresh"])
    eng.write("demo.fresh", t([1], [5.0]))
    res = eng.promote()
    assert res["promoted"] == ["demo.fresh"]
    assert rows(eng, "demo.fresh").num_rows == 1             # data now on main


def test_promote_refuses_dirty(eng):
    eng.create("dev", ["demo.orders"])
    eng.write("demo.orders", t([9], [90.0]))                 # branch write
    eng.switch("main")
    eng.write("demo.orders", t([8], [80.0]))                 # main advances
    eng.switch("dev")
    with pytest.raises(BranchError, match="main advanced"):
        eng.promote()
    assert rows(eng, "demo.orders").num_rows == 4            # nothing clobbered


def test_promote_reports_stale_pins(eng):
    eng.write("raw.events", t([1], [1.0]))
    eng.create("dev", ["demo.orders"])
    eng.write("demo.orders", t([5], [50.0]))
    eng.switch("main")
    eng.write("raw.events", t([2], [2.0]))                   # pinned input advances
    eng.switch("dev")
    res = eng.promote()
    assert res["stale_pins"] == ["raw.events"]
