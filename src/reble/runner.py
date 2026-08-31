"""reble run: execute models over branch-resolved Iceberg reads.

The whole flow (validated in spikes/04-sqlglot-direct), no framework in between:

  1. Load models (plain SQL files), fingerprint them (canonical AST + upstream
     composition — see models.py).
  2. A model is STALE when its fingerprint differs from what was last published
     in this context (branch name, or "main"). A fresh branch inherits main's
     published fingerprints — that's literally what the refs mean.
  3. Execute stale models in dependency order in an EPHEMERAL in-memory DuckDB:
     inputs are registered as views over branch-resolved Iceberg scans (pins /
     epoch), outputs are committed straight to the branch ref via the guarded
     write path. No persistent intermediate database exists.

Iceberg is the only place data lives.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import duckdb
import pyarrow as pa

from reble.branches import EPOCH_EMPTY, BranchEngine
from reble.config import RebleConfig
from reble.errors import RebleError
from reble.models import deps_of, fingerprints, load_models, topo_order
from reble.state import MAIN


@dataclass
class RunResult:
    environment: str                                       # branch name or "main"
    changed: list[str] = field(default_factory=list)       # stale models this run
    published: list[str] = field(default_factory=list)
    guard_skipped: list[str] = field(default_factory=list) # stale but out of scope


def analyze_project(cfg: RebleConfig, engine: BranchEngine
                    ) -> tuple[list[str], dict[str, str]]:
    """(models stale vs main's published state, all models) — drives scope
    inference at `branch create` time."""
    models = load_models(cfg)
    fps = fingerprints(models)
    changed = [t for t in topo_order(models)
               if fps[t] != engine.state.published_fp(MAIN, t)]
    return changed, models


def _published_fp(engine: BranchEngine, ctx: str, table: str) -> str | None:
    """A branch starts from main's published state (that's what refs mean)."""
    fp = engine.state.published_fp(ctx, table)
    if fp is None and ctx != MAIN:
        fp = engine.state.published_fp(MAIN, table)
    return fp


def _exists_at(engine: BranchEngine, table: str, manifest) -> bool:
    from pyiceberg.exceptions import NoSuchTableError
    try:
        tbl = engine.catalog.load_table(table)
    except NoSuchTableError:
        return False
    if manifest is None:
        return tbl.current_snapshot() is not None
    return manifest.name in tbl.metadata.refs


def _register_input(con, engine: BranchEngine, table: str,
                    produced: dict | None = None) -> None:
    """Expose `table` to DuckDB as the current branch sees it."""
    if produced is not None and table in produced:
        arrow = produced[table]
    else:
        snap = engine.resolve_read(table)
        tbl = engine.catalog.load_table(table)
        if snap == EPOCH_EMPTY:                     # born after the branch epoch
            arrow = tbl.scan().to_arrow().schema.empty_table()
        else:
            arrow = tbl.scan(snapshot_id=snap).to_arrow()
    schema, name = table.rsplit(".", 1)
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    con.register(f"_v_{schema}_{name}", arrow)
    con.execute(f'CREATE OR REPLACE VIEW "{schema}"."{name}" '
                f'AS SELECT * FROM "_v_{schema}_{name}"')


def query(cfg: RebleConfig, engine: BranchEngine, sql: str):
    """Ad-hoc SQL against the warehouse as the CURRENT BRANCH sees it.

    The same adapter the runner uses: referenced tables are registered as
    views over branch-resolved Iceberg scans (refs for scoped tables, pins/
    epoch for the rest), then DuckDB executes. Returns (connection, relation);
    caller displays and closes.
    """
    con = duckdb.connect(config={
        "memory_limit": "12GB",
        "temp_directory": str(cfg.project_dir / ".reble" / "tmp"),
    })
    known = set(engine._all_tables())
    for t in sorted(deps_of(sql) & known):
        _register_input(con, engine, t)
    return con, con.sql(sql)


def run(cfg: RebleConfig, engine: BranchEngine, force: bool = False) -> RunResult:
    models = load_models(cfg)
    if not models:
        raise RebleError("no models found under models/<schema>/<name>.sql")
    fps = fingerprints(models)
    order = topo_order(models)

    manifest = engine.current()
    ctx = manifest.name if manifest else MAIN
    res = RunResult(environment=ctx)

    warehouse_tables = set(engine._all_tables())
    missing = {d for t in models for d in deps_of(models[t])
               if d not in models and d not in warehouse_tables}
    if missing:
        raise RebleError(
            f"models depend on tables not in the warehouse: {sorted(missing)}")

    con = duckdb.connect(config={
        "memory_limit": "12GB",
        "temp_directory": str(cfg.project_dir / ".reble" / "tmp"),
    })
    produced: dict[str, pa.Table] = {}
    try:
        for t in order:
            stale = force or fps[t] != _published_fp(engine, ctx, t)
            if stale:
                res.changed.append(t)

            # scope routing (branches only)
            if manifest is not None and t not in manifest.scope:
                if stale and manifest.open_scope:
                    engine.grow_scope(t)               # branch-first: scope grows
                    manifest = engine.current()
                elif stale:
                    res.guard_skipped.append(t)
                    continue
                else:
                    continue                            # unchanged, not ours
            elif not stale and _exists_at(engine, t, manifest):
                continue                                # up to date, nothing to do

            # register this model's inputs as views over branch-resolved reads
            for dep in sorted(deps_of(models[t])):
                _register_input(con, engine, dep, produced)

            out = con.execute(models[t]).to_arrow_table()
            produced[t] = out
            engine.write(t, out, mode="overwrite")
            engine.state.set_published_fp(ctx, t, fps[t])
            res.published.append(t)
    finally:
        con.close()
    return res
