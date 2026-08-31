import pyarrow as pa
import pytest

from reble.branches import BranchEngine
from reble.config import RebleConfig
from reble.errors import BranchError, WriteGuardError


def t(start: int, n: int = 3) -> pa.Table:
    return pa.table({"id": pa.array(range(start, start + n), pa.int64())})


@pytest.fixture
def eng(tmp_path):
    (tmp_path / "warehouse").mkdir()
    cfg = RebleConfig(project_dir=tmp_path)
    e = BranchEngine(cfg)
    e.write("demo.orders", t(0))          # main: 3 rows
    e.write("demo.customers", t(100))     # main: 3 rows
    return e


def rows(eng, table, snapshot_id=None):
    tbl = eng.catalog.load_table(table)
    return tbl.scan(snapshot_id=snapshot_id).to_arrow().num_rows


def test_branch_isolation_and_pins(eng):
    m = eng.create("dev", ["demo.orders"])
    assert "demo.customers" in m.pins

    eng.write("demo.orders", t(10))                       # branch write
    assert rows(eng, "demo.orders") == 3                  # main untouched
    assert rows(eng, "demo.orders", eng.resolve_read("demo.orders")) == 6

    # main advances on the pinned table; the pin must hold
    eng.switch("main")
    eng.write("demo.customers", t(200))
    eng.switch("dev")
    assert rows(eng, "demo.customers") == 6               # live main
    assert rows(eng, "demo.customers", eng.resolve_read("demo.customers")) == 3


def test_write_guard(eng):
    eng.create("dev", ["demo.orders"])
    with pytest.raises(WriteGuardError):
        eng.write("demo.customers", t(999))


def test_new_model_workflow(eng):
    eng.create("feat", ["demo.new_table"])                # doesn't exist yet
    eng.write("demo.new_table", t(0))
    tbl = eng.catalog.load_table("demo.new_table")
    assert "feat" in tbl.metadata.refs                    # data on the branch ref
    assert rows(eng, "demo.new_table", eng.resolve_read("demo.new_table")) == 3


def test_delete_cleans_refs_and_current(eng):
    eng.create("dev", ["demo.orders"])
    eng.write("demo.orders", t(10))
    eng.delete("dev")
    assert "dev" not in eng.catalog.load_table("demo.orders").metadata.refs
    assert eng.state.current_branch() == "main"
    assert rows(eng, "demo.orders") == 3                  # main unaffected


def test_duplicate_and_missing(eng):
    eng.create("dev", ["demo.orders"])
    with pytest.raises(BranchError):
        eng.create("dev", ["demo.orders"])
    with pytest.raises(BranchError):
        eng.switch("nope")


def test_overlap_warning(eng):
    eng.create("a", ["demo.orders"])
    m = eng.create("b", ["demo.orders"])
    assert ("a", "demo.orders") in m.overlaps
