import pyarrow as pa
import pytest

from reble.branches import BranchEngine
from reble.config import load_config
from reble.runner import run
from reble.scaffold import scaffold

RAW_MODEL = """\
MODEL (name demo.orders_clean, kind FULL);
SELECT id, amount FROM raw.orders WHERE amount > 0
"""

TOTALS_MODEL = """\
MODEL (name demo.totals, kind FULL);
SELECT COUNT(*) AS n, SUM(amount) AS total FROM demo.orders_clean
"""


@pytest.fixture
def project(tmp_path):
    scaffold(tmp_path / "proj")
    p = tmp_path / "proj"
    # replace scaffold examples with a raw-reading model chain
    (p / "models" / "example.sql").unlink()
    (p / "models" / "example_summary.sql").unlink()
    (p / "models" / "orders_clean.sql").write_text(RAW_MODEL)
    (p / "models" / "totals.sql").write_text(TOTALS_MODEL)
    cfg = load_config(p)
    eng = BranchEngine(cfg)
    eng.write("raw.orders", pa.table({
        "id": pa.array([1, 2, 3], pa.int64()),
        "amount": pa.array([10.0, -5.0, 20.0], pa.float64()),
    }))
    return cfg, eng


def rows(eng, table, snap=None):
    return eng.catalog.load_table(table).scan(snapshot_id=snap).to_arrow()


def test_run_on_main_publishes_models(project):
    cfg, eng = project
    res = run(cfg, eng)
    assert res.environment == "prod"
    assert "raw.orders" in res.mirrored
    assert set(res.published) >= {"demo.orders_clean", "demo.totals"}
    assert rows(eng, "demo.orders_clean").num_rows == 2      # negative filtered
    assert rows(eng, "demo.totals").to_pydict()["total"] == [30.0]


def test_branch_run_isolated_and_pinned(project):
    cfg, eng = project
    run(cfg, eng)                                            # main baseline

    eng.create("fix", ["demo.orders_clean", "demo.totals"])
    # change the model on the branch
    (cfg.project_dir / "models" / "orders_clean.sql").write_text(
        "MODEL (name demo.orders_clean, kind FULL);\n"
        "SELECT id, amount FROM raw.orders   -- keep negatives now\n"
    )
    # prod ingests more raw data AFTER branching; the pin must hide it
    eng.switch("main")
    eng.write("raw.orders", pa.table({
        "id": pa.array([99], pa.int64()),
        "amount": pa.array([1000.0], pa.float64()),
    }))
    eng.switch("fix")

    res = run(cfg, eng)
    assert res.environment == "fix"
    assert "demo.orders_clean" in res.changed

    branch_rows = rows(eng, "demo.orders_clean", eng.resolve_read("demo.orders_clean"))
    assert branch_rows.num_rows == 3                         # negatives kept, pin held (not 4)
    assert rows(eng, "demo.orders_clean").num_rows == 2      # main untouched


def test_guard_reports_out_of_scope_change(project):
    cfg, eng = project
    run(cfg, eng)
    eng.create("narrow", ["demo.totals"])                    # scope misses orders_clean
    (cfg.project_dir / "models" / "orders_clean.sql").write_text(
        "MODEL (name demo.orders_clean, kind FULL);\n"
        "SELECT id, amount * 2 AS amount FROM raw.orders\n"
    )
    res = run(cfg, eng)
    assert "demo.orders_clean" in res.guard_skipped
    assert rows(eng, "demo.orders_clean").num_rows == 2      # iceberg main untouched
