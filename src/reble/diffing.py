"""reble diff: what did this branch change, per scoped table.

Two modes per table (docs/architecture.md §2, "Two workflows, one loop"):
  - diff:    branch vs the base it branched from — schema changes + row-level
             added / removed / changed counts
  - profile: a table with no meaningful main counterpart (new-model workflow) —
             schema, row count, null counts

Row identity: uses `id`, or any `*_id` column verified unique on both sides
(changed = same key, different values, NULL-safe); otherwise falls back to
multiset EXCEPT ALL over common columns (added/removed only — changed shows up
as +1/-1 pairs).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import duckdb
from pyiceberg.exceptions import NoSuchTableError

from reble.branches import BranchEngine
from reble.errors import BranchError

def _pick_key(con, common: list[str]) -> str | None:
    """`id` if present, else the first `*_id` column that is actually unique
    on BOTH sides — never guess a key that would double-count."""
    candidates = [c for c in common if c == "id"] + \
        [c for c in common if c.endswith("_id")]
    for c in candidates:
        unique = True
        for side in ("m", "b"):
            n, d = con.execute(
                f'SELECT count(*), count(DISTINCT "{c}") FROM {side}').fetchone()
            if n != d:
                unique = False
                break
        if unique:
            return c
    return None


@dataclass
class TableDiff:
    table: str
    kind: str                                   # "diff" | "profile"
    rows_main: int = 0
    rows_branch: int = 0
    added: int = 0
    removed: int = 0
    changed: int | None = None                  # None = no key column, unknowable
    key: str | None = None
    schema_added: list[str] = field(default_factory=list)
    schema_removed: list[str] = field(default_factory=list)
    profile_columns: list[tuple[str, str, int]] = field(default_factory=list)
    # (name, type, null_count) — profile mode only


def diff_branch(engine: BranchEngine) -> list[TableDiff]:
    m = engine.current()
    if m is None:
        raise BranchError("on main — nothing to diff. Switch to a branch first.")

    con = duckdb.connect(config={"memory_limit": "12GB"})
    out: list[TableDiff] = []
    for t in m.scope:
        try:
            tbl = engine.catalog.load_table(t)
        except NoSuchTableError:
            continue                             # scoped but never written
        ref = tbl.metadata.refs.get(m.name)
        if ref is None:
            continue                             # scoped but never written
        branch_arrow = tbl.scan(snapshot_id=ref.snapshot_id).to_arrow()

        base_snap = m.base.get(t)
        main_arrow = None
        if base_snap is not None:
            main_arrow = tbl.scan(snapshot_id=base_snap).to_arrow()
        else:
            cur = tbl.current_snapshot()
            if cur is not None:
                main_arrow = tbl.scan(snapshot_id=cur.snapshot_id).to_arrow()

        if main_arrow is None or main_arrow.num_rows == 0:
            out.append(_profile(con, t, branch_arrow))
        else:
            out.append(_diff(con, t, main_arrow, branch_arrow))
    con.close()
    return out


def _profile(con, table: str, arrow) -> TableDiff:
    d = TableDiff(table=table, kind="profile", rows_branch=arrow.num_rows)
    con.register("_p", arrow)
    for f in arrow.schema:
        nulls = con.execute(f'SELECT count(*) FROM _p WHERE "{f.name}" IS NULL'
                            ).fetchone()[0]
        d.profile_columns.append((f.name, str(f.type), nulls))
    con.unregister("_p")
    return d


def _diff(con, table: str, main_arrow, branch_arrow) -> TableDiff:
    d = TableDiff(table=table, kind="diff",
                  rows_main=main_arrow.num_rows, rows_branch=branch_arrow.num_rows)
    main_cols = {f.name: str(f.type) for f in main_arrow.schema}
    branch_cols = {f.name: str(f.type) for f in branch_arrow.schema}
    d.schema_added = sorted(branch_cols.keys() - main_cols.keys())
    d.schema_removed = sorted(main_cols.keys() - branch_cols.keys())
    common = sorted(main_cols.keys() & branch_cols.keys())

    con.register("m", main_arrow)
    con.register("b", branch_arrow)
    try:
        key = _pick_key(con, common)
        if key is not None:
            d.key = key
            d.added = con.execute(
                f'SELECT count(*) FROM b ANTI JOIN m USING ("{key}")'
            ).fetchone()[0]
            d.removed = con.execute(
                f'SELECT count(*) FROM m ANTI JOIN b USING ("{key}")'
            ).fetchone()[0]
            value_cols = [c for c in common if c != key]
            if value_cols:
                pred = " OR ".join(
                    f'b."{c}" IS DISTINCT FROM m."{c}"' for c in value_cols
                )
                d.changed = con.execute(
                    f'SELECT count(*) FROM b JOIN m USING ("{key}") '
                    f"WHERE {pred}"
                ).fetchone()[0]
            else:
                d.changed = 0
        else:
            cols = ", ".join(f'"{c}"' for c in common)
            d.added = con.execute(
                f"SELECT count(*) FROM (SELECT {cols} FROM b EXCEPT ALL "
                f"SELECT {cols} FROM m)").fetchone()[0]
            d.removed = con.execute(
                f"SELECT count(*) FROM (SELECT {cols} FROM m EXCEPT ALL "
                f"SELECT {cols} FROM b)").fetchone()[0]
    finally:
        con.unregister("m")
        con.unregister("b")
    return d
