"""API contract tests: the exact third-party surface Reble depends on.

Reble sits on newer-than-stable Iceberg APIs (branch writes, upsert-to-branch,
ref manipulation). These tests pin that contract explicitly so a dependency
upgrade fails HERE, in milliseconds, with a named missing capability — not
twenty minutes into a run with a cryptic error. They are the fast companion to
the spikes (spikes/*/RESULTS.md), which remain the deep regression gate for
version-set promotion.
"""
import inspect

import pytest


def params_of(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


class TestPyicebergContract:
    def test_table_write_apis_accept_branch(self):
        from pyiceberg.table import Table
        for method in ("append", "overwrite", "upsert"):
            assert hasattr(Table, method), f"Table.{method} gone"
            assert "branch" in params_of(getattr(Table, method)), \
                f"Table.{method} lost its branch parameter"

    def test_upsert_supports_join_cols(self):
        from pyiceberg.table import Table
        assert "join_cols" in params_of(Table.upsert)

    def test_write_apis_accept_snapshot_properties(self):
        # watermark caching (spike 5) rides on this
        from pyiceberg.table import Table
        for method in ("append", "upsert"):
            assert "snapshot_properties" in params_of(getattr(Table, method))

    def test_manage_snapshots_surface(self):
        from pyiceberg.table.update.snapshot import ManageSnapshots
        for method in ("create_branch", "remove_branch", "create_tag",
                       "set_current_snapshot", "commit"):
            assert hasattr(ManageSnapshots, method), f"ManageSnapshots.{method} gone"
        # promote rides on ref_name-based fast-forward
        assert {"snapshot_id", "ref_name"} <= params_of(
            ManageSnapshots.set_current_snapshot)

    def test_scan_accepts_snapshot_id_and_projection(self):
        # pins/epoch reads + the projection-pushdown design rule
        from pyiceberg.table import Table
        p = params_of(Table.scan)
        assert "snapshot_id" in p
        assert "selected_fields" in p

    def test_metadata_exposes_refs_and_snapshot_log(self):
        # branch resolution + epoch (_snapshot_as_of) ride on these
        from pyiceberg.table.metadata import TableMetadataV2
        fields = set(TableMetadataV2.model_fields)
        assert "refs" in fields
        assert "snapshot_log" in fields

    def test_schema_ride_machinery(self):
        # branch-write schema isolation depends on these staying available
        from pyiceberg.table.update import SetCurrentSchemaUpdate  # noqa: F401
        from pyiceberg.table import Table
        from pyiceberg.table.snapshots import Snapshot
        assert "schema_id" in Snapshot.model_fields
        assert hasattr(Table, "snapshot_by_id")
        assert hasattr(Table, "transaction")

    def test_sql_catalog_loadable(self):
        from pyiceberg.catalog import load_catalog  # noqa: F401  (import is the test)


class TestDuckdbContract:
    def test_arrow_roundtrip(self):
        import duckdb
        import pyarrow as pa
        con = duckdb.connect()
        con.register("t", pa.table({"x": pa.array([1, 2], pa.int64())}))
        out = con.execute("SELECT sum(x) FROM t").to_arrow_table()
        assert out.column(0).to_pylist() == [3]
        con.close()

    def test_connect_accepts_config(self):
        import duckdb
        con = duckdb.connect(config={"memory_limit": "1GB"})
        con.close()


class TestSqlglotContract:
    def test_parse_deps_and_lineage_surface(self):
        import sqlglot
        from sqlglot import exp
        from sqlglot.lineage import lineage
        tree = sqlglot.parse_one(
            "WITH v AS (SELECT a FROM raw.t) SELECT a FROM v", read="duckdb")
        tables = {t.name for t in tree.find_all(exp.Table)}
        assert "t" in tables
        node = lineage("a", "SELECT a FROM raw.t",
                       schema={"raw": {"t": {"a": "int"}}}, dialect="duckdb")
        assert any("a" in n.name for n in node.walk())

    def test_canonicalization_stable(self):
        # fingerprints ride on: cosmetic differences normalize away
        import sqlglot
        a = sqlglot.parse_one("select  x from t -- c", read="duckdb").sql(
            dialect="duckdb", comments=False)
        b = sqlglot.parse_one("SELECT x\nFROM t", read="duckdb").sql(
            dialect="duckdb", comments=False)
        assert a == b


class TestVersionSet:
    def test_certified_versions(self):
        """The certified set. Bump deliberately (spikes + suite green), never by
        accident. requirements-lock.txt is the full transitive freeze."""
        import duckdb
        import pyarrow
        import pyiceberg
        import sqlglot
        assert pyiceberg.__version__ == "0.11.1"
        assert duckdb.__version__ == "1.5.5"
        assert pyarrow.__version__ == "25.0.1"
        assert sqlglot.__version__ == "30.8.0"
