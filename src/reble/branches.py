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
from pyiceberg.table.update import SetCurrentSchemaUpdate

from reble.config import RebleConfig
from reble.errors import BranchError, WriteGuardError
from reble.state import MAIN, BranchManifest, StateStore

EPOCH_EMPTY = -1   # table did not exist (or had no data) at the branch epoch


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
    if "://" not in cfg.warehouse:      # local mode: a fresh clone has no
        (cfg.project_dir / cfg.warehouse).mkdir(parents=True, exist_ok=True)
    return load_catalog(
        "reble",
        type="sql",
        uri=cfg.resolved_catalog_uri,
        warehouse=cfg.warehouse_path,
        **cfg.catalog_properties,
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

    def _snapshot_as_of(self, table: str, epoch_s: float) -> int | None:
        """Latest snapshot committed at or before the epoch (seconds), else None."""
        try:
            tbl = self.catalog.load_table(table)
        except NoSuchTableError:
            return None
        epoch_ms = int(epoch_s * 1000)
        best = None
        for entry in tbl.metadata.snapshot_log:
            if entry.timestamp_ms <= epoch_ms:
                best = entry.snapshot_id
        return best

    # -- lifecycle -------------------------------------------------------------
    def create(self, name: str, scope: list[str],
               pin_tables: list[str] | None = None,
               open_scope: bool = False,
               git_branch: str | None = None,
               git_sha: str | None = None,
               git_dirty: bool = False) -> BranchManifest:
        """Create a subset branch.

        pin_tables: which tables to pin (lineage-scoped upstream inputs, when the
        caller knows them). None = pin every non-scoped table — the safe fallback
        when inputs can't be inferred (raw tables, external writers).

        open_scope: branch-first mode — empty scope allowed; the scope grows at
        run time and every unscoped read resolves as of the branch epoch.
        """
        if not scope and not open_scope:
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

        if open_scope:
            pins = {}   # epoch resolution covers every unscoped read lazily
        else:
            pinnable = (existing & set(pin_tables)) if pin_tables is not None \
                else existing - set(scope)
            pins = {
                t: s for t in pinnable
                if (s := self._snapshot_id(t)) is not None
            }

        m = BranchManifest(
            name=name, scope=sorted(scope), pins=pins, base=base,
            created_at=time.time(), ttl_days=self.cfg.default_branch_ttl_days,
            open_scope=open_scope,
            git_branch=git_branch, git_sha=git_sha, git_dirty=git_dirty,
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

    def promote(self) -> dict:
        """Promote the current branch to main: fast-forward each scoped table's
        main ref to the branch ref, then delete the branch.

        Refuses (whole branch, atomically — no partial promotes) when any scoped
        table's main advanced since branching: that's the dirty case, and the
        remedy is rerunning on main, never a data merge. Also reports pinned
        tables whose main advanced (stale inputs — the lineage-aware version of
        this check is future work; for now it's a warning).
        """
        m = self.current()
        if m is None:
            raise BranchError("on main — nothing to promote")

        # check everything before touching anything
        to_promote: list[tuple[str, int]] = []       # (table, branch snapshot)
        dirty: list[str] = []
        for t in m.scope:
            try:
                tbl = self.catalog.load_table(t)
            except NoSuchTableError:
                continue                              # scoped, never written
            ref = tbl.metadata.refs.get(m.name)
            if ref is None:
                continue                              # scoped, never written
            cur = tbl.current_snapshot()
            base = m.base.get(t)
            if base is not None and cur is not None and cur.snapshot_id != base:
                dirty.append(t)
            else:
                to_promote.append((t, ref.snapshot_id))
        if dirty:
            raise BranchError(
                f"cannot fast-forward: main advanced since branching for {dirty}. "
                "Switch to main and rerun the models there (reble branch rebase "
                "is coming); no data merge will ever happen."
            )

        stale_pins = [
            t for t, pinned in m.pins.items()
            if (s := self._snapshot_id(t)) is not None and s != pinned
        ]

        promoted = []
        for t, branch_snap in to_promote:
            tbl = self.catalog.load_table(t)
            tbl.manage_snapshots().set_current_snapshot(
                snapshot_id=branch_snap).commit()
            tbl = self.catalog.load_table(t)
            # promote carries the branch's schema forward with its data
            snap = tbl.snapshot_by_id(branch_snap)
            if snap is not None and snap.schema_id is not None \
                    and snap.schema_id != tbl.metadata.current_schema_id:
                self._set_current_schema_id(tbl, snap.schema_id)
                tbl = self.catalog.load_table(t)
            if m.name in tbl.metadata.refs:
                tbl.manage_snapshots().remove_branch(m.name).commit()
            promoted.append(t)

        name = m.name
        self.state.carry_published_to_main(name, promoted)
        self.state.remove(name)                       # also resets current to main
        return {"branch": name, "promoted": promoted, "stale_pins": stale_pins}

    def grow_scope(self, table: str) -> None:
        """Open-scope branches only: admit a table into the writable scope."""
        m = self.current()
        if m is None:
            raise BranchError("on main — no branch scope to grow")
        if not m.open_scope:
            raise BranchError(
                f"branch {m.name!r} has an explicit scope; {table!r} is outside it")
        self.state.add_to_scope(m.name, table)

    def gc(self) -> list[str]:
        """Delete branches past their TTL (drops refs, releases pins)."""
        expired = [m.name for m in self.state.expired()]
        for name in expired:
            self.delete(name)
        return expired

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
        # no precomputed pin: resolve as of the branch epoch (strict
        # reproducibility — the branch point is when you branched)
        snap = self._snapshot_as_of(table, m.created_at)
        return snap if snap is not None else EPOCH_EMPTY

    def _align_schema(self, tbl, df: pa.Table):
        """FULL-model overwrites own the table's schema: when a model's output
        columns change, evolve the Iceberg schema to match (add new columns,
        drop removed ones) before writing. Returns (table, changed)."""
        current = {f.name for f in tbl.schema().fields}
        new = set(df.schema.names)
        if current == new:
            return tbl, False
        with tbl.update_schema(allow_incompatible_changes=True) as upd:
            upd.union_by_name(df.schema)
            for name in current - new:
                upd.delete_column(name)
        return self.catalog.load_table(".".join(tbl.name()[-2:])), True

    def _set_current_schema_id(self, tbl, schema_id: int):
        """Schemas ride with snapshots: a branch write registers its schema
        (so its snapshots project correctly) but must not change what MAIN
        readers see. pyiceberg's high-level API always makes an evolved schema
        current, so we flip current-schema-id explicitly. Covered by the API
        contract tests; revisit on pyiceberg upgrades."""
        tx = tbl.transaction()
        tx._apply((SetCurrentSchemaUpdate(schema_id=schema_id),), ())
        tx.commit_transaction()

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
            epoch_snap = self._snapshot_as_of(table, m.created_at)
            snap = tbl.current_snapshot()
            if epoch_snap is not None:
                tbl.manage_snapshots().create_branch(epoch_snap, m.name).commit()
                self.state.update_base(m.name, table, epoch_snap)
                tbl = self.catalog.load_table(table)
                snap = None  # handled
            elif snap is not None:
                # table born after the epoch: treat as new on this branch —
                # branch from an empty state is impossible per-table, so seed
                # the ref at current but record base for the clean/dirty check
                tbl.manage_snapshots().create_branch(snap.snapshot_id, m.name).commit()
                self.state.update_base(m.name, table, snap.snapshot_id)
                tbl = self.catalog.load_table(table)
                snap = None  # handled
            if snap is None and m.name not in tbl.metadata.refs:
                # brand-new table: Iceberg requires the first commit on main, so
                # seed an EMPTY snapshot there (name + schema visible, zero rows),
                # then branch from it — the data itself lands only on the ref
                tbl.append(df.schema.empty_table())
                tbl = self.catalog.load_table(table)
                seeded = tbl.current_snapshot()
                tbl.manage_snapshots().create_branch(seeded.snapshot_id, m.name).commit()
                self.state.update_base(m.name, table, seeded.snapshot_id)
                tbl = self.catalog.load_table(table)
        if mode == "overwrite":
            pre_schema_id = tbl.metadata.current_schema_id
            tbl, schema_changed = self._align_schema(tbl, df)
            with _quiet_overwrite():
                tbl.overwrite(df, branch=m.name)
            if schema_changed:
                # branch snapshots carry the new schema; main keeps seeing
                # the old one until promote
                self._set_current_schema_id(
                    self.catalog.load_table(table), pre_schema_id)
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
            tbl, _ = self._align_schema(tbl, df)   # on main, evolving forward is correct
            with _quiet_overwrite():
                tbl.overwrite(df)
        else:
            tbl.append(df)
