import time

import pyarrow as pa
import pytest

from reble.branches import EPOCH_EMPTY, BranchEngine
from reble.config import RebleConfig, load_config
from reble.errors import BranchError
from reble.runner import run
from reble.scaffold import scaffold

CLEAN = "SELECT id, amount FROM raw.orders WHERE amount > 0\n"


def t(ids, amounts):
    return pa.table({"id": pa.array(ids, pa.int64()),
                     "amount": pa.array(amounts, pa.float64())})


def rows(eng, table, snap=None):
    return eng.catalog.load_table(table).scan(snapshot_id=snap).to_arrow()


@pytest.fixture
def project(tmp_path):
    scaffold(tmp_path / "p")
    p = tmp_path / "p"
    for f in (p / "models" / "demo").glob("*.sql"):
        f.unlink()
    (p / "models" / "demo" / "orders_clean.sql").write_text(CLEAN)
    cfg = load_config(p)
    eng = BranchEngine(cfg)
    eng.write("raw.orders", t([1, 2], [10.0, -5.0]))
    run(cfg, eng)                                            # prod baseline
    return cfg, eng


def test_branch_first_lazy_scope_and_epoch(project):
    cfg, eng = project
    m = eng.create("first", [], open_scope=True)             # clean tree, git-style
    assert m.scope == [] and m.open_scope

    # prod ingests AFTER the branch point
    eng.switch("main")
    eng.write("raw.orders", t([3], [99.0]))
    eng.switch("first")

    # now edit and run — scope grows, inputs resolve as of the EPOCH
    (cfg.project_dir / "models" / "demo" / "orders_clean.sql").write_text(
        CLEAN.replace("> 0", ">= -100"))
    res = run(cfg, eng)
    assert res.guard_skipped == []
    assert "demo.orders_clean" in eng.current().scope        # scope grew
    branch_rows = rows(eng, "demo.orders_clean",
                       eng.resolve_read("demo.orders_clean"))
    assert branch_rows.num_rows == 2                         # epoch inputs: not 3
    assert rows(eng, "demo.orders_clean").num_rows == 1      # main untouched

    res2 = eng.promote()                                     # grown branch promotes
    assert res2["promoted"] == ["demo.orders_clean"]


def test_epoch_empty_for_tables_born_later(project):
    cfg, eng = project
    eng.create("first", [], open_scope=True)
    eng.switch("main")
    eng.write("raw.born_later", t([1], [1.0]))
    eng.switch("first")
    assert eng.resolve_read("raw.born_later") == EPOCH_EMPTY


def test_explicit_scope_still_guards(project):
    cfg, eng = project
    m = eng.create("strict", ["demo.orders_clean"])          # explicit, not open
    assert not m.open_scope
    with pytest.raises(BranchError):
        eng.grow_scope("raw.orders")


def test_gc_deletes_expired(tmp_path):
    (tmp_path / "warehouse").mkdir()
    cfg = RebleConfig(project_dir=tmp_path, default_branch_ttl_days=0)
    eng = BranchEngine(cfg)
    eng.write("demo.x", t([1], [1.0]))
    eng.create("old", ["demo.x"])
    eng.switch("main")
    time.sleep(0.01)                                         # ttl 0 -> expired
    assert eng.gc() == ["old"]
    assert eng.state.get("old") is None
    assert "old" not in eng.catalog.load_table("demo.x").metadata.refs
