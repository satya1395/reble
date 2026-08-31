"""The branch engine: scoped refs + pins over a pyiceberg catalog.

Semantics (see docs/architecture.md §2):
- a branch's SCOPED tables get an Iceberg branch ref named after the branch
  (zero-copy, copy-on-write; writes land on the ref, main never sees them)
- every OTHER table is readable but PINNED to its snapshot at branch creation
- writes to out-of-scope tables from a branch context are refused (write guard)
- a scoped table may not exist yet (the new-model workflow): it is created on
  first write, with an empty main and the data on the branch ref
"""
from __future__ import annotations

import time
import warnings
from contextlib import contextmanager

import pyarrow as pa
from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.exceptions import NoSuchTableError

from reble.config import RebleConfig
from reble.errors import BranchError, WriteGuardError
from reble.state import MAIN, BranchManifest, StateStore


@contextmanager
def _quiet_overwrite():
    """pyiceberg warns 'Delete operation did not match any records' when
    overwriting an empty table — routine on a model's first publish."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="Delete operation did not match any records"
        )
        yield


def open_catalog(cfg: RebleConfig) -> Catalog:
    return load_catalog(
        "reble",
        type="sql",
        uri=cfg.resolved_catalog_uri,
        warehouse=cfg.warehouse_path,
    )


class BranchEngine:
    def __init__(self, cfg: RebleConfig, catalog: Catalog | None = None,
                 state: StateStore | None = None):
        self.cfg = cfg
        self.catalog = catalog or open_catalog(cfg)
        self.state = state or StateStore(cfg.state_path)

    # -- helpers ---------------------------------------------------------------
    def _all_tables(self) -> list[str]:
        return [
            ".".join(ident)
            for ns in self.catalog.list_namespaces()
            for ident in self.catalog.list_tables(ns)
        ]

    def _snapshot_id(self, table: str) -> int | None:
        tbl = self.catalog.load_table(table)
        snap = tbl.current_snapshot()
        return snap.snapshot_id if snap else None

    # -- lifecycle -------------------------------------------------------------
    def create(self, name: str, scope: list[str]) -> BranchManifest:
        if not scope:
            raise BranchError("a branch needs a scope: pass the tables you'll change")
        existing = set(self._all_tables())

        # warn-worthy overlap: another live branch already scopes one of these tables
        overlaps = [
            (m.name, t) for m in self.state.list() for t in m.scope if t in scope
        ]

        base: dict[str, int] = {}
        for t in scope:
            if t in existing:
                snap = self._snapshot_id(t)
                if snap is not None:
                    tbl = self.catalog.load_table(t)
                    tbl.manage_snapshots().create_branch(snap, name).commit()
                    base[t] = snap
                # empty table (no snapshot yet): ref is created on first write
            # not in catalog yet: new-model workflow, created on first write

        pins = {
            t: s for t in existing - set(scope)
            if (s := self._snapshot_id(t)) is not None
        }

        m = BranchManifest(
            name=name, scope=sorted(scope), pins=pins, base=base,
            created_at=time.time(), ttl_days=self.cfg.default_branch_ttl_days,
        )
        self.state.add(m)
        self.state.set_current(name)
        m.overlaps = overlaps  # advisory, surfaced by the CLI
        return m

    def delete(self, name: str) -> None:
        m = self.state.get(name)
        if m is None:
            raise BranchError(f"branch {name!r} does not exist")
        for t in m.scope:
            try:
                tbl = self.catalog.load_table(t)
            except NoSuchTableError:
                continue
            if name in tbl.metadata.refs:
                tbl.manage_snapshots().remove_branch(name).commit()
        self.state.remove(name)

    def switch(self, name: str) -> None:
        self.state.set_current(name)

    def current(self) -> BranchManifest | None:
        """Manifest of the current branch; None when on main."""
        cur = self.state.current_branch()
        return None if cur == MAIN else self.state.get(cur)

    # -- resolution ------------------------------------------------------------
    def resolve_read(self, table: str) -> int | None:
        """Snapshot id a read of `table` should use on the current branch.

        None means 'current main state' (on main, or a scoped-but-unwritten table).
        """
        m = self.current()
        if m is None:
            return None
        if table in m.scope:
            try:
                tbl = self.catalog.load_table(table)
            except NoSuchTableError:
                return None
            ref = tbl.metadata.refs.get(m.name)
            return ref.snapshot_id if ref else None
        if table in m.pins:
            return m.pins[table]
        return None  # table created on main after branching; read current

    # -- guarded writes --------------------------------------------------------
    def write(self, table: str, df: pa.Table, mode: str = "append") -> None:
        """Write `df` to `table`, routed and guarded by the current branch.

        mode: "append" adds rows; "overwrite" replaces the table's contents
        (model outputs use overwrite). On main: plain write. On a branch: table
        must be in scope; the write lands on the branch ref (creating table
        and/or ref if needed).
        """
        m = self.current()
        if m is None:
            self._write_main(table, df, mode)
            return
        if table not in m.scope:
            raise WriteGuardError(
                f"refusing write to {table!r}: not in branch {m.name!r} scope "
                f"{m.scope}. Add it to the branch scope or switch to main."
            )
        try:
            tbl = self.catalog.load_table(table)
        except NoSuchTableError:
            ns = table.rsplit(".", 1)[0]
            self.catalog.create_namespace_if_not_exists(ns)
            tbl = self.catalog.create_table(table, schema=df.schema)
        if m.name not in tbl.metadata.refs:
            snap = tbl.current_snapshot()
            if snap is None:
                # brand-new table: Iceberg requires the first commit on main, so
                # seed an EMPTY snapshot there (name + schema visible, zero rows),
                # then branch from it — the data itself lands only on the ref
                tbl.append(df.schema.empty_table())
                tbl = self.catalog.load_table(table)
                snap = tbl.current_snapshot()
            tbl.manage_snapshots().create_branch(snap.snapshot_id, m.name).commit()
            tbl = self.catalog.load_table(table)
        if mode == "overwrite":
            with _quiet_overwrite():
                tbl.overwrite(df, branch=m.name)
        else:
            tbl.append(df, branch=m.name)

    def _write_main(self, table: str, df: pa.Table, mode: str = "append") -> None:
        try:
            tbl = self.catalog.load_table(table)
        except NoSuchTableError:
            ns = table.rsplit(".", 1)[0]
            self.catalog.create_namespace_if_not_exists(ns)
            tbl = self.catalog.create_table(table, schema=df.schema)
        if mode == "overwrite":
            with _quiet_overwrite():
                tbl.overwrite(df)
        else:
            tbl.append(df)
