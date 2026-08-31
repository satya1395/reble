import pyarrow as pa
import pytest

from reble.branches import BranchEngine
from reble.config import load_config
from reble.runner import analyze_project, run, upstream_closure
from reble.scaffold import scaffold

CLEAN = """\
MODEL (name demo.orders_clean, kind FULL);
SELECT id, amount FROM raw.orders WHERE amount > 0
"""

TOTALS = """\
MODEL (name demo.totals, kind FULL);
SELECT COUNT(*) AS n, SUM(amount) AS total FROM demo.orders_clean
"""

UNRELATED = """\
MODEL (name demo.unrelated, kind FULL);
SELECT 1 AS x
"""


@pytest.fixture
def project(tmp_path):
    scaffold(tmp_path / "p")
    p = tmp_path / "p"
    for f in (p / "models").glob("*.sql"):
        f.unlink()
    (p / "models" / "orders_clean.sql").write_text(CLEAN)
    (p / "models" / "totals.sql").write_text(TOTALS)
    (p / "models" / "unrelated.sql").write_text(UNRELATED)
    cfg = load_config(p)
    eng = BranchEngine(cfg)
    eng.write("raw.orders", pa.table({
        "id": pa.array([1, 2], pa.int64()),
        "amount": pa.array([10.0, 20.0], pa.float64()),
    }))
    eng.write("raw.untouched", pa.table({"k": pa.array([1], pa.int64())}))
    run(cfg, eng)                                    # prod baseline
    return cfg, eng


def test_analyze_detects_change_and_cascade(project):
    cfg, eng = project
    changed, deps = analyze_project(cfg)
    assert changed == []                             # clean tree

    (cfg.project_dir / "models" / "orders_clean.sql").write_text(
        CLEAN.replace("> 0", ">= 0"))
    changed, deps = analyze_project(cfg)
    assert "demo.orders_clean" in changed
    assert "demo.totals" in changed                  # downstream cascade included
    assert "demo.unrelated" not in changed


def test_upstream_closure_lineage_scoped(project):
    cfg, _ = project
    _, deps = analyze_project(cfg)
    pins = upstream_closure(["demo.orders_clean", "demo.totals"], deps)
    assert pins == ["raw.orders"]                    # NOT raw.untouched or unrelated


def test_upstream_closure_unknown_table_falls_back(project):
    cfg, _ = project
    _, deps = analyze_project(cfg)
    assert upstream_closure(["raw.orders"], deps) is None   # not a model: pin-all


def test_create_with_lineage_pins(project):
    cfg, eng = project
    _, deps = analyze_project(cfg)
    pins = upstream_closure(["demo.totals", "demo.orders_clean"], deps)
    m = eng.create("dev", ["demo.totals", "demo.orders_clean"], pin_tables=pins)
    assert set(m.pins) == {"raw.orders"}             # lineage-scoped, not everything
