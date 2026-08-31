import pyarrow as pa
import pytest

from reble.branches import BranchEngine
from reble.config import load_config
from reble.models import upstream_closure
from reble.runner import analyze_project, run
from reble.scaffold import scaffold

CLEAN = "SELECT id, amount FROM raw.orders WHERE amount > 0\n"
TOTALS = "SELECT COUNT(*) AS n, SUM(amount) AS total FROM demo.orders_clean\n"
UNRELATED = "SELECT 1 AS x\n"


@pytest.fixture
def project(tmp_path):
    scaffold(tmp_path / "p")
    p = tmp_path / "p"
    for f in (p / "models" / "demo").glob("*.sql"):
        f.unlink()
    (p / "models" / "demo" / "orders_clean.sql").write_text(CLEAN)
    (p / "models" / "demo" / "totals.sql").write_text(TOTALS)
    (p / "models" / "demo" / "unrelated.sql").write_text(UNRELATED)
    cfg = load_config(p)
    eng = BranchEngine(cfg)
    eng.write("raw.orders", pa.table({
        "id": pa.array([1, 2], pa.int64()),
        "amount": pa.array([10.0, 20.0], pa.float64())}))
    eng.write("raw.untouched", pa.table({"k": pa.array([1], pa.int64())}))
    run(cfg, eng)
    return cfg, eng


def test_analyze_detects_change_and_cascade(project):
    cfg, eng = project
    changed, _ = analyze_project(cfg, eng)
    assert changed == []                                     # clean tree

    (cfg.project_dir / "models" / "demo" / "orders_clean.sql").write_text(
        CLEAN.replace("> 0", ">= 0"))
    changed, _ = analyze_project(cfg, eng)
    assert "demo.orders_clean" in changed
    assert "demo.totals" in changed                          # cascade
    assert "demo.unrelated" not in changed


def test_create_with_lineage_pins(project):
    cfg, eng = project
    _, models = analyze_project(cfg, eng)
    pins = upstream_closure(["demo.totals", "demo.orders_clean"], models)
    assert pins == ["raw.orders"]                            # not raw.untouched
    m = eng.create("dev", ["demo.totals", "demo.orders_clean"], pin_tables=pins)
    assert set(m.pins) == {"raw.orders"}
