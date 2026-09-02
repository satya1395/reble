"""examples/orders-lakehouse is runnable as documented — kept honest by CI."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from reble.cli import app

EXAMPLE = Path(__file__).parent.parent / "examples" / "orders-lakehouse"
runner = CliRunner()


def test_example_walkthrough(tmp_path, monkeypatch):
    """The README's setup + loop, executed for real."""
    work = tmp_path / "orders-lakehouse"
    shutil.copytree(EXAMPLE, work)
    monkeypatch.chdir(work)

    # Setup: init + seed
    result = runner.invoke(app, ["init", "--catalog", "sql", "--namespace", "analytics"])
    assert result.exit_code == 0, result.stdout
    seeded = subprocess.run(
        [sys.executable, "seed.py"], capture_output=True, text=True, check=False
    )
    assert seeded.returncode == 0, seeded.stderr
    assert "3 rows" in seeded.stdout

    # First run with explicit models (fresh branch: empty scope otherwise)
    result = runner.invoke(app, ["run", "--models", "stg_orders,mart_orders,report_daily"])
    assert result.exit_code == 0, result.stdout
    assert "stg_orders: ran" in result.stdout

    # The loop: edit a model, scoped re-run, diff, promote
    stg = work / "models" / "stg_orders.sql"
    stg.write_text(stg.read_text().replace("amount > 0", "amount > 15"))
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.stdout
    # downstream closure pulled mart_orders + report_daily into scope
    assert "mart_orders: ran" in result.stdout

    diff = runner.invoke(app, ["--json", "diff", "mart_orders"])
    assert diff.exit_code == 0, diff.stdout
    import json

    table = json.loads(diff.stdout)["data"]["tables"][0]
    # the models were never promoted, so main holds only the empty seed:
    # the branch diff vs base is the full result of the tightened filter
    assert table["added"] == 2  # orders 2,3 (amount 20,30) clear amount > 15

    result = runner.invoke(app, ["promote"])
    assert result.exit_code == 0, result.stdout
    assert "no merge, ever" in result.stdout

    # main now carries the promoted branch state
    import yaml as _yaml
    from pyiceberg.catalog import load_catalog

    cfg = _yaml.safe_load((work / "reble.yml").read_text())
    cat = load_catalog("reble", **cfg["warehouse"]["catalog"])
    rows = cat.load_table("analytics.mart_orders").scan().to_arrow().to_pylist()
    assert [r["order_id"] for r in rows] == [2, 3]
