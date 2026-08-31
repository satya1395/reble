"""reble run: SQLMesh plan/apply with branch context.

Flow (validated in spikes/03-sqlmesh-embedding):
  1. Mirror branch-resolved Iceberg inputs into SQLMesh's DuckDB db — every table
     a model reads that SQLMesh doesn't itself manage (raw/source tables) is
     loaded at the snapshot the current branch resolves it to (pins!), so model
     inputs are stable while prod ingests.
  2. SQLMesh plan/apply — on main to prod; on a branch to a SQLMesh environment
     named after the branch (reble branch <-> SQLMesh env, 1:1). Only changed
     models physically run.
  3. Publish model outputs to Iceberg through the guarded write path — on a
     branch, only in-scope tables; the guard makes out-of-scope changes loud.
"""
from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass, field

import duckdb
import pyarrow as pa

from reble.branches import BranchEngine
from reble.config import RebleConfig
from reble.errors import RebleError

SQLMESH_DB = ".reble/db.db"


def _env_name(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", branch)


def _norm(ident: str) -> str:
    """'"db"."demo"."orders"' -> 'demo.orders' (schema.table, quotes stripped)."""
    parts = [p.strip('"') for p in ident.split(".")]
    return ".".join(parts[-2:])


def _exists_at(engine: BranchEngine, table: str, manifest) -> bool:
    """The model's output physically exists where this run would put it."""
    from pyiceberg.exceptions import NoSuchTableError
    try:
        tbl = engine.catalog.load_table(table)
    except NoSuchTableError:
        return False
    if manifest is None:
        return tbl.current_snapshot() is not None
    return manifest.name in tbl.metadata.refs


@dataclass
class RunResult:
    environment: str
    changed: list[str] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    mirrored: list[str] = field(default_factory=list)
    guard_skipped: list[str] = field(default_factory=list)   # changed but out of scope


def analyze_project(cfg: RebleConfig) -> tuple[list[str], dict[str, set[str]]]:
    """What changed vs prod, and the model dependency graph.

    Returns (changed_tables, deps) where changed_tables includes the downstream
    cascade (SQLMesh plans indirectly-modified models too) and deps maps each
    model's output table to the tables it reads.
    """
    from sqlmesh.core.context import Context
    with _in_dir(cfg.project_dir), contextlib.closing(
        Context(paths=[str(cfg.project_dir)])
    ) as ctx:
        deps = {
            _norm(m.name): {_norm(str(d)) for d in getattr(m, "depends_on", set()) or set()}
            for m in ctx.models.values()
        }
        plan = ctx.plan(no_prompts=True, auto_apply=False)
        changed = sorted({_norm(s.name) for s in plan.new_snapshots})
    return changed, deps


def upstream_closure(scope: list[str], deps: dict[str, set[str]]) -> list[str] | None:
    """All tables the scoped models transitively read (excluding the scope).

    Returns None when a scoped table isn't a known model — its inputs are
    unknowable from lineage, so the caller should fall back to pinning
    everything (safe over clever).
    """
    if any(t not in deps for t in scope):
        return None
    seen: set[str] = set()
    stack = list(scope)
    while stack:
        for d in deps.get(stack.pop(), ()):
            if d not in seen and d not in scope:
                seen.add(d)
                stack.append(d)
    return sorted(seen)


@contextlib.contextmanager
def _in_dir(path):
    """SQLMesh resolves its relative duckdb path against cwd, not the project dir."""
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def run(cfg: RebleConfig, engine: BranchEngine) -> RunResult:
    from sqlmesh.core.context import Context

    manifest = engine.current()
    env = _env_name(manifest.name) if manifest else "prod"
    res = RunResult(environment=env)

    # -- discover models and their external (non-model) dependencies ----------
    with _in_dir(cfg.project_dir):
        ctx = Context(paths=[str(cfg.project_dir)])
        model_tables = {_norm(m.name) for m in ctx.models.values()}
        external: set[str] = set()
        for m in ctx.models.values():
            for dep in getattr(m, "depends_on", set()) or set():
                d = _norm(str(dep))
                if d not in model_tables:
                    external.add(d)
        ctx.close()   # release the duckdb file before we open our own connection

    # -- 1. mirror branch-resolved external inputs into SQLMesh's duckdb ------
    catalog_tables = set(engine._all_tables())
    to_mirror = sorted(external & catalog_tables)
    if to_mirror:
        con = duckdb.connect(str(cfg.project_dir / SQLMESH_DB))
        try:
            for t in to_mirror:
                snap = engine.resolve_read(t)
                arrow = engine.catalog.load_table(t).scan(snapshot_id=snap).to_arrow()
                schema, name = t.rsplit(".", 1)
                con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                con.register("_reble_mirror", arrow)
                con.execute(
                    f'CREATE OR REPLACE TABLE "{schema}"."{name}" '
                    "AS SELECT * FROM _reble_mirror"
                )
                con.unregister("_reble_mirror")
                res.mirrored.append(t)
        finally:
            con.close()
    missing = external - catalog_tables
    if missing:
        raise RebleError(
            f"models depend on tables not in the warehouse: {sorted(missing)}"
        )

    # -- 2. plan/apply --------------------------------------------------------
    with _in_dir(cfg.project_dir), contextlib.closing(
        Context(paths=[str(cfg.project_dir)])
    ) as ctx:
        if manifest:
            plan = ctx.plan(environment=env, auto_apply=True, no_prompts=True,
                            include_unmodified=True)
        else:
            plan = ctx.plan(auto_apply=True, no_prompts=True)
        res.changed = sorted(_norm(s.name) for s in plan.new_snapshots)

        # sqlmesh snapshot identifier per model output table: SQLMesh reuses
        # snapshots across environments, so "did the plan create new snapshots"
        # is NOT "is my published copy current" — compare fingerprints instead
        fps = {_norm(name): str(s.identifier) for name, s in ctx.snapshots.items()}
        pub_ctx = manifest.name if manifest else "main"

        # -- 3. publish model outputs to Iceberg (guarded) --------------------
        for m in ctx.models.values():
            target = _norm(m.name)
            fp = fps.get(target)
            stale = fp is None or fp != engine.state.published_fp(pub_ctx, target)
            if manifest and target not in manifest.scope:
                if stale:
                    res.guard_skipped.append(target)
                continue
            if not stale and _exists_at(engine, target, manifest):
                continue   # this exact snapshot version is already published there
            schema, name = target.rsplit(".", 1)
            src_schema = f"{schema}__{env}" if manifest else schema
            df = ctx.fetchdf(f'SELECT * FROM "{src_schema}"."{name}"')
            engine.write(target, pa.Table.from_pandas(df), mode="overwrite")
            if fp is not None:
                engine.state.set_published_fp(pub_ctx, target, fp)
            res.published.append(target)
    return res
