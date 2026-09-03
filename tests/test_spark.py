"""SparkEngine end-to-end against a real Spark session.

Gated: needs a JVM + pyspark + a Postgres catalog, so these run only with
REBLE_SPARK_TESTS=1 (the CI spark job and developer machines). The harness
points pyiceberg and Spark's JdbcCatalog at the *same* Postgres database —
refs created by either side are visible to both. (SQLite can't play this
role: Spark's JDBC pool holds a database-level lock that blocks pyiceberg.)
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.spark,
    pytest.mark.skipif(
        os.environ.get("REBLE_SPARK_TESTS") != "1",
        reason="set REBLE_SPARK_TESTS=1 (requires JVM + pyspark + Postgres)",
    ),
]

PG_URI = os.environ.get(
    "REBLE_SPARK_PG_URI", "postgresql://reble@127.0.0.1:55432/reble_spark"
)


@pytest.fixture()
def spark_project(project):
    """The standard git project, but with the catalog on Postgres."""
    reble_yml = (project / "reble.yml").read_text()
    reble_yml = reble_yml.replace(
        f"uri: sqlite:///{project}/catalog.db", f"uri: {PG_URI}"
    )
    (project / "reble.yml").write_text(reble_yml)
    return project


@pytest.fixture()
def spark_core(spark_project):
    from reble.catalog import load_catalog as reble_load_catalog
    from reble.config import ConfigLoader
    from reble.core import Reble

    cfg = ConfigLoader(spark_project).load()
    # The Postgres catalog persists across runs — reset its bookkeeping
    # before any catalog instance exists (pyiceberg creates the tables
    # lazily in its constructor).
    from sqlalchemy import create_engine, text

    engine = create_engine(PG_URI)
    with engine.begin() as conn:
        for table in ("iceberg_tables", "iceberg_namespace_properties"):
            conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    engine.dispose()
    catalog = reble_load_catalog(cfg.warehouse.catalog)
    catalog.create_namespace_if_not_exists("analytics")
    import pyarrow as pa

    table = catalog.create_table(
        "analytics.raw_events",
        schema=pa.schema([("order_id", pa.int64()), ("amount", pa.float64())]),
    )
    table.append(
        pa.table({"order_id": [1, 2, 3], "amount": [10.0, 20.0, 30.0]})
    )
    core = Reble(spark_project)
    yield core
    # Drop the Spark session so the next test's catalog reset isn't served
    # from cached table state in the same JVM.
    try:
        from pyspark.sql import SparkSession

        session = SparkSession.getActiveSession()
        if session is not None:
            session.stop()
    except Exception:  # noqa: BLE001
        pass


def _rows_on(catalog, table_id, ref):
    from reble.catalog import get_head

    head = get_head(catalog, table_id, ref)
    return catalog.load_table(table_id).scan(snapshot_id=head).to_arrow().to_pylist()


def _catalog(spark_core):
    from reble.catalog import load_catalog

    return load_catalog(spark_core.cfg.warehouse.catalog)


def test_spark_run_writes_branch_not_main(spark_core):
    out = spark_core.run(engine="spark")
    assert out["ok"] is True

    cat = _catalog(spark_core)
    # branch head has the edited model's rows — the fix-orders edit widens
    # the filter from `amount > 15` (2 rows) to `amount > 5` (all 3)
    branch_rows = _rows_on(cat, "analytics.stg_orders", out["branch"]["data"])
    assert len(branch_rows) == 3
    # main never received data — only the zero-row seed
    main_rows = _rows_on(cat, "analytics.stg_orders", "main")
    assert main_rows == []


def test_spark_provenance_in_snapshot_summary(spark_core):
    from reble.catalog import get_head

    out = spark_core.run(engine="spark")
    cat = _catalog(spark_core)
    table = cat.load_table("analytics.stg_orders")
    head = get_head(cat, "analytics.stg_orders", out["branch"]["data"])
    snapshot = table.snapshot_by_id(head)
    props = dict(getattr(snapshot.summary, "additional_properties", {}) or {})
    assert props.get("reble.model") == "stg_orders"
    assert props.get("reble.branch") == out["branch"]["data"]
    assert props.get("reble.ast_hash")


def test_spark_full_lifecycle_run_diff_promote(spark_core):
    run = spark_core.run(engine="spark")
    assert run["ok"] is True

    diff = spark_core.diff()
    assert diff["ok"] is True

    promoted = spark_core.promote()
    assert promoted["ok"] is True
    cat = _catalog(spark_core)
    main_rows = _rows_on(cat, "analytics.stg_orders", "main")
    assert len(main_rows) == 3


def test_spark_rerun_replaces_never_appends(spark_core):

    first = spark_core.run(engine="spark", force=True)
    assert first["ok"] is True
    second = spark_core.run(engine="spark", force=True)
    assert second["ok"] is True

    cat = _catalog(spark_core)
    branch_rows = _rows_on(cat, "analytics.stg_orders", second["branch"]["data"])
    assert len(branch_rows) == 3  # replaced, not appended (would be 6)


def test_sparkio_version_as_of_view(spark_core):
    from reble.catalog import get_head
    from reble.sparkio import SparkIo

    io = SparkIo(spark_core.cfg.engines.spark, spark_core.cfg.warehouse.catalog)
    spark = io.connect()
    head = get_head(_catalog(spark_core), "analytics.raw_events", "main")
    io.register_snapshot("v_head", "analytics.raw_events", head)
    assert spark.sql("SELECT COUNT(*) AS n FROM v_head").first()["n"] == 3
