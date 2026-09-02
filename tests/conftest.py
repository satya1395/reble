"""Shared fixtures: a standalone-SQL project + a local SQL Iceberg catalog."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pyarrow as pa
import pytest


@pytest.fixture()
def agent_project(tmp_path, monkeypatch):
    """Standalone project, git_sync: false, NO git repo — the agent case."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "warehouse").mkdir()
    (tmp_path / "models" / "stg_orders.sql").write_text(
        "-- kind: table\n-- key: order_id\n"
        "select * from raw_events where amount > 15\n"
    )
    (tmp_path / "models" / "mart_orders.sql").write_text(
        "-- kind: table\n-- key: order_id\n"
        "select order_id, amount * 2 as amount_doubled from stg_orders\n"
    )
    (tmp_path / "reble.yml").write_text(
        f"""
version: 1
warehouse:
  catalog:
    type: sql
    uri: sqlite:///{tmp_path}/catalog.db
    warehouse: file://{tmp_path}/warehouse
  namespace: analytics
  default_base: main
branching:
  git_sync: false
lineage:
  models_path: models
  dialect: duckdb
"""
    )
    return tmp_path


RAW_EVENTS_ROWS = {"order_id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]}

MODELS = {
    "stg_orders.sql": (
        "-- kind: table\n"
        "select * from raw_events where amount > 5\n"
    ),
    "mart_orders.sql": (
        "-- kind: table\n"
        "-- key: order_id\n"
        "select order_id, amount * 2 as amount_doubled from stg_orders\n"
    ),
    "report_daily.sql": (
        "-- kind: view\n"
        "select order_id from mart_orders\n"
    ),
}


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A git repo with reble.yml + models/, cwd switched into it.

    main has stg_orders filtered `amount > 15`; the working tree on branch
    fix-orders edits it to `amount > 5` (uncommitted) — the real
    "branch, edit, run" gesture.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "stg_orders.sql").write_text(
        "-- kind: table\nselect * from raw_events where amount > 15\n"
    )
    for name, sql in MODELS.items():
        if name != "stg_orders.sql":
            (tmp_path / "models" / name).write_text(sql)
    (tmp_path / ".gitignore").write_text("")
    _git_ok(tmp_path, "init", "-b", "main")
    _git_ok(tmp_path, "add", "-A")
    _git_ok(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    _git_ok(tmp_path, "checkout", "-qb", "fix-orders")
    (tmp_path / "models" / "stg_orders.sql").write_text(
        "-- kind: table\nselect * from raw_events where amount > 5\n"
    )
    (tmp_path / "reble.yml").write_text(
        f"""
version: 1
warehouse:
  catalog:
    type: sql
    uri: sqlite:///{tmp_path}/catalog.db
    warehouse: file://{tmp_path}/warehouse
  namespace: analytics
  default_base: main
lineage:
  models_path: models
  dialect: duckdb
"""
    )
    (tmp_path / "warehouse").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture()
def seeded_catalog(project):
    """Catalog with analytics.raw_events on main (3 rows)."""
    from reble.catalog import load_catalog as reble_load_catalog
    from reble.config import ConfigLoader

    cfg = ConfigLoader(project).load()
    catalog = reble_load_catalog(cfg.warehouse.catalog)
    catalog.create_namespace_if_not_exists("analytics")
    table = catalog.create_table(
        "analytics.raw_events",
        schema=pa.schema([("order_id", pa.int64()), ("amount", pa.float64())]),
    )
    table.append(pa.table(RAW_EVENTS_ROWS))
    return catalog


def _git_ok(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def rows_of(catalog, table_id: str) -> list[dict]:
    return catalog.load_table(table_id).scan().to_arrow().to_pylist()


def main_head(catalog, table_id: str) -> int:
    from reble.catalog import get_head

    return get_head(catalog, table_id, "main")
