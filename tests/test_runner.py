import pyarrow as pa
import pytest

from reble.branches import BranchEngine
from reble.config import load_config
from reble.runner import run
from reble.scaffold import scaffold

CLEAN = "SELECT id, amount FROM raw.orders WHERE amount > 0\n"
TOTALS = "SELECT COUNT(*) AS n, SUM(amount) AS total FROM demo.orders_clean\n"


@pytest.fixture
def project(tmp_path):
    scaffold(tmp_path / "proj")
    p = tmp_path / "proj"
    for f in (p / "models" / "demo").glob("*.sql"):
        f.unlink()
    (p / "models" / "demo" / "orders_clean.sql").write_text(CLEAN)
    (p / "models" / "demo" / "totals.sql").write_text(TOTALS)
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
    assert res.environment == "main"
    assert set(res.published) == {"demo.orders_clean", "demo.totals"}
    assert rows(eng, "demo.orders_clean").num_rows == 2
    assert rows(eng, "demo.totals").to_pydict()["total"] == [30.0]

    res2 = run(cfg, eng)                                     # idempotent
    assert res2.published == [] and res2.changed == []

    res3 = run(cfg, eng, force=True)                         # data-refresh path
    assert set(res3.published) == {"demo.orders_clean", "demo.totals"}


def test_branch_run_isolated_and_pinned(project):
    cfg, eng = project
    run(cfg, eng)

    eng.create("fix", ["demo.orders_clean", "demo.totals"])
    (cfg.project_dir / "models" / "demo" / "orders_clean.sql").write_text(
        "SELECT id, amount FROM raw.orders   -- keep negatives now\n")
    eng.switch("main")
    eng.write("raw.orders", pa.table({                       # prod ingests after epoch
        "id": pa.array([99], pa.int64()),
        "amount": pa.array([1000.0], pa.float64())}))
    eng.switch("fix")

    res = run(cfg, eng)
    assert "demo.orders_clean" in res.changed
    branch_rows = rows(eng, "demo.orders_clean",
                       eng.resolve_read("demo.orders_clean"))
    assert branch_rows.num_rows == 3                         # pin held: not 4
    assert rows(eng, "demo.orders_clean").num_rows == 2      # main untouched


def test_guard_reports_out_of_scope_change(project):
    cfg, eng = project
    run(cfg, eng)
    eng.create("narrow", ["demo.totals"])                    # misses orders_clean
    (cfg.project_dir / "models" / "demo" / "orders_clean.sql").write_text(
        "SELECT id, amount * 2 AS amount FROM raw.orders\n")
    res = run(cfg, eng)
    assert "demo.orders_clean" in res.guard_skipped
    assert rows(eng, "demo.orders_clean").num_rows == 2      # main untouched


def test_main_run_after_branch_promote_refreshes_downstream(project):
    cfg, eng = project
    run(cfg, eng)

    eng.create("fix", ["demo.orders_clean"])                 # totals NOT in scope
    (cfg.project_dir / "models" / "demo" / "orders_clean.sql").write_text(
        "SELECT id, amount FROM raw.orders   -- keep negatives\n")
    run(cfg, eng)
    eng.promote()

    res = run(cfg, eng)                                      # back on main
    assert "demo.totals" in res.published                    # downstream refreshed
    assert "demo.orders_clean" not in res.published          # fast-forwarded, fp carried
    assert rows(eng, "demo.totals").to_pydict()["total"] == [25.0]

    assert run(cfg, eng).published == []                     # idempotent


def test_cli_load_csv(tmp_path):
    from click.testing import CliRunner
    from reble.cli import cli as reble_cli
    from reble.scaffold import scaffold
    scaffold(tmp_path / "p")
    (tmp_path / "p" / "seeds").mkdir()
    (tmp_path / "p" / "seeds" / "orders.csv").write_text(
        "id,amount\n1,10.5\n2,20.0\n")
    r = CliRunner()
    import os
    old = os.getcwd()
    os.chdir(tmp_path / "p")
    try:
        out = r.invoke(reble_cli, ["load", "raw.orders", "seeds/orders.csv"])
        assert out.exit_code == 0, out.output
        assert "Loaded 2 rows into raw.orders" in out.output
        out2 = r.invoke(reble_cli, ["load", "raw.orders", "seeds/orders.csv",
                                    "--overwrite"])
        assert out2.exit_code == 0, out2.output
    finally:
        os.chdir(old)
    from reble.branches import BranchEngine
    from reble.config import load_config
    eng = BranchEngine(load_config(tmp_path / "p"))
    assert eng.catalog.load_table("raw.orders").scan().to_arrow().num_rows == 2


def test_query_sees_branch_and_pins(project):
    from reble.runner import query
    cfg, eng = project
    run(cfg, eng)
    eng.create("q", ["demo.orders_clean"])
    (cfg.project_dir / "models" / "demo" / "orders_clean.sql").write_text(
        "SELECT id, amount FROM raw.orders   -- keep negatives\n")
    run(cfg, eng)

    con, rel = query(cfg, eng, "SELECT count(*) AS n FROM demo.orders_clean")
    assert rel.fetchall() == [(3,)]                          # branch view
    con.close()

    eng.switch("main")
    con, rel = query(cfg, eng, "SELECT count(*) AS n FROM demo.orders_clean")
    assert rel.fetchall() == [(2,)]                          # main view
    con.close()

    con, rel = query(cfg, eng, "SELECT 1 + 1")               # no tables: fine
    assert rel.fetchall() == [(2,)]
    con.close()
