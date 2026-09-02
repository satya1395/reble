"""Row-level + schema diff (spec section 4, `reble diff`).

Runs on the user's compute. Key columns come from config/dbt tests; fallback
is full-hash compare with a warning (on_missing_key).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import duckdb
import pyarrow as pa

from .errors import MissingDiffKey

_NULL = "'\\N'"


@dataclass
class TableDiff:
    table: str
    key_columns: list[str] | None  # None means full-hash compare
    added_count: int = 0
    removed_count: int = 0
    changed_count: int = 0
    unchanged_count: int = 0
    schema_added: list[str] = field(default_factory=list)
    schema_removed: list[str] = field(default_factory=list)
    schema_changed: list[str] = field(default_factory=list)
    added_samples: list[dict] = field(default_factory=list)
    removed_samples: list[dict] = field(default_factory=list)
    changed_samples: list[dict] = field(default_factory=list)
    warning: str | None = None

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "key_columns": self.key_columns,
            "added": self.added_count,
            "removed": self.removed_count,
            "changed": self.changed_count,
            "unchanged": self.unchanged_count,
            "schema_delta": {
                "added": self.schema_added,
                "removed": self.schema_removed,
                "changed": self.schema_changed,
            },
            "samples": {
                "added": self.added_samples,
                "removed": self.removed_samples,
                "changed": self.changed_samples,
            },
            "warning": self.warning,
        }


def resolve_keys(
    table: str,
    explicit_keys: dict[str, list[str]],
    inferred_keys: list[str],
    on_missing_key: str,
) -> list[str] | None:
    """Explicit config keys win; else dbt-inferred; else hash fallback or error (exit 7)."""
    if table in explicit_keys:
        return explicit_keys[table]
    if inferred_keys:
        return inferred_keys
    if on_missing_key == "error":
        raise MissingDiffKey(f"{table}: no diff key (on_missing_key: error)")
    return None


def diff_arrow(
    table_name: str,
    base: pa.Table,
    branch: pa.Table,
    keys: list[str] | None,
    max_rows_dumped: int = 1000,
) -> TableDiff:
    """Diff two snapshots of a table via DuckDB.

    keys given → anti/join on keys: added / removed / changed (same key,
    different values). keys=None → full-row-hash compare + warning.
    """
    result = TableDiff(table=table_name, key_columns=keys)
    if keys is None:
        result.warning = f"{table_name}: no diff key, full-hash compare"
    _schema_diff(base, branch, result)

    con = duckdb.connect(":memory:")
    try:
        con.register("base_tbl", base)
        con.register("branch_tbl", branch)
        _row_diff(con, result, max_rows_dumped)
    finally:
        con.close()
    return result


def _schema_diff(base: pa.Table, branch: pa.Table, result: TableDiff) -> None:
    base_cols = {f.name: str(f.type) for f in base.schema}
    branch_cols = {f.name: str(f.type) for f in branch.schema}
    result.schema_added = sorted(set(branch_cols) - set(base_cols))
    result.schema_removed = sorted(set(base_cols) - set(branch_cols))
    result.schema_changed = sorted(
        name for name in set(base_cols) & set(branch_cols)
        if base_cols[name] != branch_cols[name]
    )


def _columns(con: duckdb.DuckDBPyConnection, view: str) -> list[str]:
    return [row[0] for row in con.execute(f"DESCRIBE {view}").fetchall()]


def _row_diff(con: duckdb.DuckDBPyConnection, result: TableDiff, max_rows: int) -> None:
    limit = f" LIMIT {int(max_rows)}" if max_rows and max_rows > 0 else ""
    base_cols = _columns(con, "base_tbl")
    branch_cols = _columns(con, "branch_tbl")
    common = [c for c in branch_cols if c in base_cols]
    select_common = ", ".join(f'"{c}"' for c in common) or "*"

    if result.key_columns:
        keys = [k for k in result.key_columns if k in common]
        on = " AND ".join(f'b."{k}" IS NOT DISTINCT FROM a."{k}"' for k in keys)
        nonkey = [c for c in common if c not in keys]
        differs = " OR ".join(
            f'NOT (b."{c}" IS NOT DISTINCT FROM a."{c}")' for c in nonkey
        ) or "FALSE"
        b_select = ", ".join(f'b."{c}"' for c in common) or "*"
        a_select = ", ".join(f'a."{c}"' for c in common) or "*"

        result.added_count = con.execute(
            f"SELECT COUNT(*) FROM branch_tbl b ANTI JOIN base_tbl a ON ({on})"
        ).fetchone()[0]
        result.removed_count = con.execute(
            f"SELECT COUNT(*) FROM base_tbl a ANTI JOIN branch_tbl b ON ({on})"
        ).fetchone()[0]
        result.changed_count = con.execute(
            f"SELECT COUNT(*) FROM branch_tbl b JOIN base_tbl a ON ({on}) WHERE {differs}"
        ).fetchone()[0]
        result.unchanged_count = con.execute(
            f"SELECT COUNT(*) FROM branch_tbl b JOIN base_tbl a ON ({on}) "
            f"WHERE NOT ({differs})"
        ).fetchone()[0]
        added_rows = con.execute(
            f"SELECT {b_select} FROM branch_tbl b ANTI JOIN base_tbl a ON ({on}){limit}"
        ).fetchall()
        removed_rows = con.execute(
            f"SELECT {a_select} FROM base_tbl a ANTI JOIN branch_tbl b ON ({on}){limit}"
        ).fetchall()
        changed_rows = con.execute(
            f"SELECT {b_select} FROM branch_tbl b JOIN base_tbl a ON ({on}) "
            f"WHERE {differs}{limit}"
        ).fetchall()
    else:
        branch_hash = _row_hash(branch_cols)
        base_hash = _row_hash(base_cols)
        result.added_count = con.execute(
            f"SELECT COUNT(*) FROM branch_tbl WHERE {branch_hash} NOT IN "
            f"(SELECT {base_hash} FROM base_tbl)"
        ).fetchone()[0]
        result.removed_count = con.execute(
            f"SELECT COUNT(*) FROM base_tbl WHERE {base_hash} NOT IN "
            f"(SELECT {branch_hash} FROM branch_tbl)"
        ).fetchone()[0]
        # Row-granular changed attribution requires a key; hash mode is multiset.
        result.changed_count = 0
        result.unchanged_count = 0
        added_rows = con.execute(
            f"SELECT {select_common} FROM branch_tbl WHERE {branch_hash} NOT IN "
            f"(SELECT {base_hash} FROM base_tbl){limit}"
        ).fetchall()
        removed_rows = con.execute(
            f"SELECT {select_common} FROM base_tbl WHERE {base_hash} NOT IN "
            f"(SELECT {branch_hash} FROM branch_tbl){limit}"
        ).fetchall()
        changed_rows = []

    result.added_samples = _to_dicts(common or branch_cols, added_rows)
    result.removed_samples = _to_dicts(common or base_cols, removed_rows)
    result.changed_samples = _to_dicts(common, changed_rows)


def _row_hash(cols: list[str]) -> str:
    parts = " || ".join(
        f"COALESCE(CAST(\"{c}\" AS VARCHAR), {_NULL})" for c in cols
    )
    return f"MD5({parts})"


def _to_dicts(cols: list[str], rows: list[tuple]) -> list[dict]:
    return [
        {c: _jsonable(v) for c, v in zip(cols, row, strict=False)}
        for row in rows
    ]


def _jsonable(value):
    import decimal

    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    return value
