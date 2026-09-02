"""Reble CLI (spec sections 4–6).

Global flags: --config PATH, --profile NAME, --json, --no-color, --quiet.
Exit codes are normative (spec section 6) and carried by RebleError.
"""

from __future__ import annotations

import functools
import json
import time
from pathlib import Path

import typer

from . import __version__, envelope
from .catalog import drop_ref, ensure_branch, get_head, get_ref_snapshot, load_catalog
from .config import ConfigLoader, assert_no_secrets
from .diff import diff_arrow, resolve_keys
from .engine import DuckDbEngine, SparkEngine
from .errors import EXIT_DRIFT, EXIT_ERROR, ConfigError, EmptyScope, RebleError
from .gitinfo import (
    base_commit,
    current_branch,
    file_at,
    head_commit,
    repo_root,
    uncommitted_files,
)
from .lineage import ast_hash, build_graph
from .naming import disambiguate, sanitize_branch_name
from .promote import Promoter, orphan_pin_tags
from .relations import relation_id, table_for_model, tag_name
from .runner import Runner
from .scope import compute_scope
from .state import BranchState, Pin, StateStore

app = typer.Typer(
    name="reble",
    help="Git-style branching for Iceberg data warehouses — bring your own SQL.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
branch_app = typer.Typer(help="Explicit branch management (required when git_sync: false).")
app.add_typer(branch_app, name="branch")


def _run_guard(func):
    """Translate RebleError into spec exit codes; anything else is exit 1."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RebleError as exc:
            typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(exc.exit_code) from exc
        except (typer.Exit, SystemExit):
            raise  # deliberate exits (e.g. status drift → 3) pass through
        except Exception as exc:
            typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_ERROR) from exc

    return wrapper


class Context:
    def __init__(self, config_path: Path | None, profile: str | None):
        self.project_root = (config_path.parent if config_path else Path.cwd()).resolve()
        self.loader = ConfigLoader(self.project_root)
        self.cfg = self.loader.load(profile=profile)
        self.reble_dir = self.loader.reble_dir
        self.store = StateStore(self.reble_dir)
        self.state = self.store.load()
        self.catalog = load_catalog(self.cfg.warehouse.catalog)
        engine_cls = (
            SparkEngine if self.cfg.compute_policy.prefer == "spark" else DuckDbEngine
        )
        self.engine = engine_cls(self.cfg, self.catalog)

    @property
    def git_branch(self) -> str | None:
        """Invariant 1: reble reads git, never runs it — and git_sync: false
        makes reble 100% git-ignorant."""
        if not self.cfg.branching.git_sync:
            return None
        root = repo_root(self.project_root)
        return current_branch(root) if root else None

    def data_branch_for(self, git_branch: str | None, explicit: str | None = None) -> str:
        if explicit:
            return explicit
        if git_branch is None:
            raise ConfigError(
                "No git branch available. Create an explicit data branch: "
                "reble branch create <name>"
            )
        # The recorded git↔data mapping wins: our own branch's existing refs
        # are not a collision. Disambiguation is only for a genuinely new
        # branch whose sanitized name is taken (e.g. another engineer's).
        st = self.state.branches.get(git_branch)
        if st is not None:
            return st.data_branch
        sanitized = sanitize_branch_name(git_branch, self.cfg.branching.name_sanitization)
        return disambiguate(sanitized, self._existing_refs())

    def _existing_refs(self) -> set[str]:
        refs: set[str] = set()
        for table_id in _list_tables(self.catalog):
            try:
                refs.update(self.catalog.load_table(table_id).refs().keys())
            except Exception:  # noqa: BLE001
                continue
        return refs

    def branch_state(self, git_branch: str | None, data_branch: str) -> BranchState:
        key = git_branch or data_branch
        st = self.state.branches.get(key)
        if st is None:
            raise ConfigError(
                f"No data branch for '{key}'. Run `reble run` or `reble branch create` first."
            )
        return st

    def graph(self):
        return build_graph(
            self.project_root / self.cfg.lineage.models_path, self.cfg.lineage.dialect
        )

    def edited_models(self, graph, st: BranchState | None) -> tuple[list[str], dict[str, str]]:
        """Edited = git-diff vs base commit (git_sync) ∪ AST-changed vs stored hashes."""
        edited: set[str] = set()
        dialect = self.cfg.lineage.dialect
        hashes = {n: ast_hash(m.sql, dialect) for n, m in graph.models.items() if m.sql.strip()}

        if self.cfg.branching.git_sync:
            root = repo_root(self.project_root)
            base = base_commit(root) if root else None
            if root and base:
                for name, model in graph.models.items():
                    old_sql = file_at(root, base, _relpath(model.path, root))
                    if old_sql is None or ast_hash(old_sql, dialect) != hashes[name]:
                        edited.add(name)

        if st is not None:
            for name, h in hashes.items():
                if st.model_hashes.get(name) not in (None, h) :
                    # ran before with a different SQL → edited since last run
                    edited.add(name)

        return sorted(edited), hashes


def _relpath(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root))
    except ValueError:
        return path


def _list_tables(catalog) -> list[str]:
    out: list[str] = []
    try:
        for ns in catalog.list_namespaces():
            out.extend(catalog.list_tables(ns))
    except Exception:  # noqa: BLE001
        try:
            out.extend(catalog.list_tables())
        except Exception:  # noqa: BLE001
            pass
    return out


def _print_version(value: bool):
    if value:
        typer.echo(f"reble {__version__}")
        raise typer.Exit(0)


# Global flags set by the root callback — spec: "Global flags on every
# command". A command-local flag ORs with the global one.
_FLAGS = {"json": False, "quiet": False}


def _as_json(local: bool) -> bool:
    return local or _FLAGS["json"]


def _show_text(local_json: bool, local_quiet: bool) -> bool:
    """Whether human-readable output should be printed."""
    return not (_as_json(local_json) or local_quiet or _FLAGS["quiet"])


@app.callback()
def main(
    config: Path | None = typer.Option(None, "--config", help="Path to reble.yml"),
    profile: str | None = typer.Option(None, "--profile", help="Profile from reble.yml"),
    json_output: bool = typer.Option(False, "--json", help="Stable JSON envelope"),
    no_color: bool = typer.Option(False, "--no-color"),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    version: bool = typer.Option(False, "--version", callback=_print_version, is_eager=True),
):  # pragma: no cover — flag plumbing only
    _FLAGS["json"] = json_output
    _FLAGS["quiet"] = _FLAGS["quiet"] or quiet
    _ = (no_color,)


def _emit(env: dict, as_json: bool) -> None:
    if _as_json(as_json):
        typer.echo(json.dumps(env, indent=2, default=str))
    else:
        # Text mode prints scalar summaries only; structures go to --json
        for key, value in env["data"].items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                typer.echo(f"{key}: {value}")
        for w in env["warnings"]:
            typer.secho(f"WARN {w}", fg=typer.colors.YELLOW, err=True)
        for e in env["errors"]:
            typer.secho(f"ERROR {e}", fg=typer.colors.RED, err=True)


@app.command()
@_run_guard
def init(
    catalog_type: str = typer.Option(
        "rest", "--catalog", help="glue|polaris|nessie|hive|rest|reble"
    ),
    engine: str = typer.Option("duckdb", "--engine", help="duckdb|spark"),
    namespace: str = typer.Option(
        None, "--namespace", help="Iceberg namespace for model tables"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Write reble.yml, gitignore .reble/, probe catalog, detect models/."""
    root = (config_path.parent if config_path else Path.cwd()).resolve()
    loader = ConfigLoader(root)

    detected: dict[str, str] = {}
    assumptions: list[str] = []
    models_dir = root / "models"
    if models_dir.is_dir():
        count = len(list(models_dir.rglob("*.sql")))
        detected["models"] = f"{count} SQL models under {models_dir.relative_to(root)}/"
        if count == 0:
            assumptions.append("models/ exists but contains no .sql files")
    else:
        assumptions.append(
            "no models/ directory — set lineage.models_path in reble.yml"
        )

    if loader.config_path.exists() and not yes:
        raise ConfigError(f"{loader.config_path} already exists — pass --yes to overwrite")

    raw = {
        "version": 1,
        "warehouse": {
            "catalog": {"type": catalog_type},
            "default_base": "main",
        },
        "lineage": {},
        "engines": {"duckdb": {}, "spark": {}},
        "compute_policy": {"prefer": engine},
    }
    if catalog_type == "sql":
        # working local defaults: sqlite catalog + file warehouse
        raw["warehouse"]["catalog"].update(
            {
                "uri": f"sqlite:///{root / 'catalog.db'}",
                "warehouse": f"file://{root / 'warehouse'}",
            }
        )
        (root / "warehouse").mkdir(exist_ok=True)
        assumptions.append("sql catalog: local sqlite + ./warehouse (dev defaults)")
    elif catalog_type == "in-memory":
        raw["warehouse"]["catalog"].update({"warehouse": f"file://{root / 'warehouse'}"})
        (root / "warehouse").mkdir(exist_ok=True)
        assumptions.append("in-memory catalog does not persist — per-process only")
    if namespace:
        raw["warehouse"]["namespace"] = namespace
    assert_no_secrets(raw)
    loader.config_path.write_text(_dump_yaml(raw))

    # .reble/ is machine-local (spec section 2); init adds the gitignore entry
    loader.reble_dir.mkdir(exist_ok=True)
    gitignore = root / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    if ".reble/" not in lines:
        gitignore.write_text("\n".join([*lines, ".reble/", ""]) if lines else ".reble/\n")

    # Probe catalog connectivity through the config we just wrote — also
    # validates it. Exit 2 if unreachable (spec section 4).
    probe = load_catalog(loader.load().warehouse.catalog)
    _ = _list_tables(probe)

    _emit(
        envelope.envelope(
            "init",
            ok=True,
            data={
                "config": str(loader.config_path),
                "detected": detected,
                "assumptions": assumptions,
            },
        ),
        json_output,
    )
    if _show_text(json_output, quiet):
        typer.echo(f"Wrote {loader.config_path}")
        for k, v in detected.items():
            typer.echo(f"detected: {k} = {v}")
        for a in assumptions:
            typer.secho(f"assumed: {a}", fg=typer.colors.YELLOW)
        typer.echo("catalog: reachable")


@app.command()
@_run_guard
def run(
    models: str | None = typer.Option(None, "--models", help="Comma-separated model names"),
    depth: int | None = typer.Option(None, "--depth", help="Cap downstream cascade"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    engine_name: str | None = typer.Option(None, "--engine", help="duckdb|spark"),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Resolve scope, create/update the data branch, pin inputs, execute."""
    ctx = Context(config_path, profile)
    if engine_name == "spark":
        ctx.engine = SparkEngine(ctx.cfg, ctx.catalog)
    git_branch = ctx.git_branch
    data_branch = ctx.data_branch_for(git_branch)
    graph = ctx.graph()

    key = git_branch or data_branch
    st = ctx.state.branches.get(key)
    edited, _ = ctx.edited_models(graph, st)
    if models:
        edited = [m.strip() for m in models.split(",") if m.strip()]

    scope = compute_scope(graph, edited, depth=depth)

    # Branch-first with an empty scope is legal (invariant 5): register state.
    if st is None:
        root = repo_root(ctx.project_root)
        st = BranchState(
            git_branch=git_branch or "",
            data_branch=data_branch,
            base_ref=ctx.cfg.warehouse.default_base,
            base_commit=head_commit(root) if root else None,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        ctx.state.branches[key] = st
    st.scope = scope.scope
    # Persist before execution: a crashed run must not lose the git↔data
    # branch mapping or the branch epoch (invariant 5).
    ctx.store.save(ctx.state)

    runner = Runner(ctx.cfg, ctx.catalog, graph, ctx.engine, ctx.reble_dir)

    if scope.is_empty:
        _emit(
            envelope.envelope(
                "run",
                ok=True,
                data={"status": "empty scope — branch registered (invariant 6)"},
                branch={"git": git_branch, "data": data_branch},
            ),
            json_output,
        )
        return

    preflight = runner.preflight(data_branch, scope)
    _print_preflight(preflight, not _show_text(json_output, quiet))
    warnings = [
        f"{m}: no diff key (on_missing_key: {ctx.cfg.diff.on_missing_key})"
        for m in scope.scope
        if not _keys_for(ctx, graph, m)
    ]

    if dry_run:
        _emit(
            envelope.envelope(
                "run",
                ok=True,
                data={"dry_run": True, "preflight": _jsonable(preflight)},
                branch={"git": git_branch, "data": data_branch},
                warnings=warnings,
            ),
            json_output,
        )
        return

    manifest = runner.run(data_branch, scope, st.base_ref, st.model_hashes)

    # Record execution hashes, input pins, and scope-table base heads.
    st.model_hashes.update(
        {r.model: r.ast_hash for r in manifest.results if r.ast_hash}
    )
    for table in scope.pinned_inputs:
        table_id = relation_id(ctx.cfg, table)
        snapshot = get_head(ctx.catalog, table_id, st.base_ref)
        if snapshot is not None:
            st.pins[table] = Pin(
                table=table_id,
                tag=tag_name(ctx.cfg, data_branch, table),
                snapshot_id=snapshot,
                base_snapshot_id=snapshot,
            )
    for model in scope.scope:
        table_id = table_for_model(ctx.cfg, model)
        head = get_head(ctx.catalog, table_id, st.base_ref)
        branch_head = get_head(ctx.catalog, table_id, data_branch)
        if head is not None or branch_head is not None:
            st.base_heads[table_id] = head if head is not None else -1
    st.last_run_id = manifest.run_id
    ctx.store.save(ctx.state)

    if _show_text(json_output, quiet):
        for result in manifest.results:
            line = f"{result.model}: {result.status}"
            if result.status == "ran":
                line += f" ({result.rows_written} rows, {result.duration_ms}ms)"
            if result.error:
                line += f" — {result.error}"
            typer.echo(line)
        for model in scope.incremental_full_refresh:
            typer.secho(
                f"full-refresh: {model} (incremental models always full-refresh in branches)",
                fg=typer.colors.YELLOW,
            )
    _emit(
        envelope.envelope(
            "run",
            ok=all(r.status != "error" for r in manifest.results),
            data=manifest.to_dict(),
            branch={"git": git_branch, "data": data_branch},
            warnings=warnings,
        ),
        json_output,
    )
    failed = [r for r in manifest.results if r.status == "error"]
    if failed:
        raise RebleError(
            f"run failed for {len(failed)} model(s): "
            + ", ".join(f"{r.model} ({r.error})" for r in failed)
        )


def _keys_for(ctx, graph, model_name: str) -> list[str]:
    model = graph.models.get(model_name)
    inferred = model.diff_keys if model else []
    explicit = ctx.cfg.diff.keys.get(model_name)
    return explicit or inferred


def _print_preflight(preflight: dict, quiet: bool) -> None:
    if quiet:
        return
    for key, value in preflight.items():
        if not isinstance(value, tuple):
            typer.echo(f"{key:<18}     {value}")
            continue
        count, detail = value
        if isinstance(detail, list):
            shown = (", ".join(str(d) for d in detail[:3]) + ", ...") if len(detail) > 3 else (
                ", ".join(str(d) for d in detail) or "-"
            )
        else:
            shown = str(detail)
        typer.echo(f"{key:<18} {count:>3}   {shown}")


@app.command(name="diff")
@_run_guard
def diff_cmd(
    tables: str | None = typer.Argument(None, help="Comma-separated tables (default: scope)"),
    against: str = typer.Option(
        "base", "--against", help="base (branch point) | main (advisory promote preview)"
    ),
    schema_only: bool = typer.Option(False, "--schema-only"),
    rows: int | None = typer.Option(None, "--rows"),
    full: bool = typer.Option(False, "--full", help="Ignore max_rows_dumped"),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Row-level + schema diff of scope tables (exit 7 if key required and missing)."""
    ctx = Context(config_path, profile)
    git_branch = ctx.git_branch
    data_branch = ctx.data_branch_for(git_branch)
    st = ctx.branch_state(git_branch, data_branch)
    graph = ctx.graph()

    targets = [t.strip() for t in tables.split(",")] if tables else st.scope
    if not targets:
        raise EmptyScope("no scope to diff — run `reble run` first")

    base_ref = st.base_ref if against == "base" else against
    max_rows = 0 if full else (rows if rows is not None else ctx.cfg.diff.max_rows_dumped)


    out = []
    for name in targets:
        table_id = relation_id(ctx.cfg, name)
        table = ctx.catalog.load_table(table_id)
        branch_snap = get_ref_snapshot(table, data_branch)
        base_snap = get_ref_snapshot(table, base_ref)
        if branch_snap is None:
            raise RebleError(f"{table_id}: no branch ref '{data_branch}' to diff against")
        branch_data = table.scan(snapshot_id=branch_snap).to_arrow()
        base_data = (
            table.scan(snapshot_id=base_snap).to_arrow()
            if base_snap is not None
            else _empty_like(branch_data)
        )

        model = graph.models.get(name)
        inferred = model.diff_keys if model else []
        keys = resolve_keys(name, ctx.cfg.diff.keys, inferred, ctx.cfg.diff.on_missing_key)
        if schema_only:
            d = diff_arrow(table_id, base_data, branch_data, keys=None, max_rows_dumped=0)
            d.added_count = d.removed_count = d.changed_count = 0
            d.added_samples = d.removed_samples = d.changed_samples = []
            d.warning = None
        else:
            d = diff_arrow(table_id, base_data, branch_data, keys, max_rows_dumped=max_rows)
        out.append(d.to_dict())

    _emit(
        envelope.envelope(
            "diff", ok=True, data={"tables": out}, branch={"git": git_branch, "data": data_branch}
        ),
        json_output,
    )


def _empty_like(table):
    import pyarrow as pa

    return pa.table({f.name: [] for f in table.schema}, schema=table.schema)


@app.command()
@_run_guard
def status(
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """The 'where was I?' answer. Read-only, CI-safe. Exit 3 on drift."""
    ctx = Context(config_path, profile)
    git_branch = ctx.git_branch
    data_branch = ctx.data_branch_for(git_branch)
    key = git_branch or data_branch
    st = ctx.state.branches.get(key)

    code_section: dict = {}
    data_section: dict = {}
    warnings: list[str] = []
    drift = False

    if ctx.cfg.branching.git_sync:
        root = repo_root(ctx.project_root)
        code_section["uncommitted"] = uncommitted_files(root) if root else []

    if st is None:
        data_section["branch"] = None
        warnings.append("no data branch yet — run `reble run`")
    else:
        data_section["branch"] = st.data_branch
        data_section["last_run_id"] = st.last_run_id
        data_section["scope"] = st.scope
        graph = ctx.graph()
        edited, hashes = ctx.edited_models(graph, st)
        data_section["edited_since_branch_point"] = edited
        # Un-run = edited models whose current SQL does not match the last run
        data_section["un_run_changes"] = [
            m for m in edited if st.model_hashes.get(m) != hashes.get(m)
        ]

        promoter = Promoter(ctx.cfg, ctx.catalog, ctx.reble_dir)
        drifted = [r for r in promoter.preflight(st) if r.drifted]
        if drifted:
            drift = True
            data_section["drifted"] = [
                {
                    "table": r.table,
                    "kind": r.kind,
                    "expected": r.pinned_base,
                    "current_main": r.current_main,
                }
                for r in drifted
            ]
            warnings += [
                f"{r.table}: {r.kind} base {r.pinned_base} != current main {r.current_main}"
                for r in drifted
            ]
        data_section["age_days"] = round((time.time() - st.epoch) / 86400, 1)
        data_section["expires_in_days"] = round(
            max(0, ctx.cfg.branching.ttl_days - data_section["age_days"]), 1
        )

    _emit(
        envelope.envelope(
            "status",
            ok=not drift,
            data={"code": code_section, "data": data_section},
            branch={"git": git_branch, "data": data_branch},
            warnings=warnings,
        ),
        json_output,
    )
    if drift:
        raise typer.Exit(EXIT_DRIFT)


@app.command(name="promote")
@_run_guard
def promote_cmd(
    yes: bool = typer.Option(False, "--yes", "-y"),
    ff_only: bool = typer.Option(False, "--ff-only"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Fast-forward promote — only when pinned bases still equal current main."""
    ctx = Context(config_path, profile)
    git_branch = ctx.git_branch
    data_branch = ctx.data_branch_for(git_branch)
    st = ctx.branch_state(git_branch, data_branch)

    promoter = Promoter(
        ctx.cfg, ctx.catalog, ctx.reble_dir, persist=lambda: ctx.store.save(ctx.state)
    )
    reports = promoter.preflight(st)
    drifted = [r for r in reports if r.drifted]

    if dry_run:
        _emit(
            envelope.envelope(
                "promote",
                ok=True,
                data={
                    "dry_run": True,
                    "preflight": [
                        {
                            "table": r.table,
                            "kind": r.kind,
                            "pinned_base": r.pinned_base,
                            "current_main": r.current_main,
                        }
                        for r in reports
                    ],
                    "would": "fast-forward" if not drifted else "re-run + fresh diff + fast-forward",
                    "atomicity": "per-table fast-forwards (atomic on a reble catalog)",
                },
                branch={"git": git_branch, "data": data_branch},
            ),
            json_output,
        )
        return

    promote_diff: dict[str, dict] = {}
    if drifted:
        typer.secho(
            f"drift on {len(drifted)} tables — re-pinning, re-running scope, "
            "emitting a fresh promote-time diff",
            fg=typer.colors.YELLOW,
        )
        if ff_only:
            raise RebleError(
                "promote blocked: drift detected and --ff-only given (exit 4)", exit_code=4
            )
        graph = ctx.graph()
        scope = compute_scope(graph, st.scope)
        runner = Runner(ctx.cfg, ctx.catalog, graph, ctx.engine, ctx.reble_dir)
        manifest = runner.run(data_branch, scope, st.base_ref, {})
        for table in scope.pinned_inputs:
            table_id = relation_id(ctx.cfg, table)
            snapshot = get_head(ctx.catalog, table_id, st.base_ref)
            if snapshot is not None:
                st.pins[table] = Pin(
                    table=table_id,
                    tag=tag_name(ctx.cfg, data_branch, table),
                    snapshot_id=snapshot,
                    base_snapshot_id=snapshot,
                )
        for model in scope.scope:
            table_id = table_for_model(ctx.cfg, model)
            head = get_head(ctx.catalog, table_id, st.base_ref)
            if head is not None:
                st.base_heads[table_id] = head
        st.model_hashes.update({r.model: r.ast_hash for r in manifest.results if r.ast_hash})
        ctx.store.save(ctx.state)

        # Authoritative promote-time diff (PR diffs are advisory; this is not).
        for model in scope.scope:
            table_id = table_for_model(ctx.cfg, model)
            try:
                table = ctx.catalog.load_table(table_id)
                b = get_ref_snapshot(table, data_branch)
                m = get_ref_snapshot(table, st.base_ref)
                if b is not None and m is not None:
                    branch_data = table.scan(snapshot_id=b).to_arrow()
                    base_data = table.scan(snapshot_id=m).to_arrow()
                    model_node = graph.models.get(model)
                    keys = resolve_keys(
                        model,
                        ctx.cfg.diff.keys,
                        model_node.diff_keys if model_node else [],
                        ctx.cfg.diff.on_missing_key,
                    )
                    d = diff_arrow(
                        table_id, base_data, branch_data, keys,
                        max_rows_dumped=ctx.cfg.diff.max_rows_dumped,
                    )
                    promote_diff[table_id] = d.to_dict()
            except RebleError:
                raise
            except Exception as exc:  # noqa: BLE001
                promote_diff[table_id] = {"error": str(exc)}

    results = promoter.promote(st, ff_only=ff_only)
    # The promoter advanced base heads per table (and persisted via callback);
    # persist once more so any non-table state is durable even on partial failure.
    ctx.store.save(ctx.state)
    if all(r.get("status") != "failed" for r in results.values()):
        # Fully promoted: the branch's pins advance to main too, so
        # `reble status` is clean (the branch epoch advances to the promote
        # point). Base heads were already advanced per table.
        for pin in st.pins.values():
            head = get_head(ctx.catalog, pin.table, st.base_ref)
            if head is not None:
                pin.snapshot_id = head
                pin.base_snapshot_id = head
        ctx.store.save(ctx.state)
    if _show_text(json_output, quiet):
        typer.echo("promote complete (per-table fast-forwards; no merge, ever)")
        for table_id, r in results.items():
            line = f"  {table_id}: {r.get('status')}"
            if r.get("reason"):
                line += f": {r['reason']}"
            typer.echo(line)
    _emit(
        envelope.envelope(
            "promote",
            ok=all(r.get("status") != "failed" for r in results.values()),
            data={
                "results": results,
                "promote_diff": promote_diff,
                "atomicity": "per-table fast-forwards (atomic on a reble catalog)",
            },
            branch={"git": git_branch, "data": data_branch},
        ),
        json_output,
    )


@branch_app.command(name="create")
@_run_guard
def branch_create(
    name: str = typer.Argument(..., help="Explicit data branch name"),
    from_ref: str = typer.Option(
        None, "--from", help="Ref to fork from (default: warehouse default_base)"
    ),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Explicit branch (required when git_sync: false). Zero-copy refs."""
    ctx = Context(config_path, profile)
    base_ref = from_ref or ctx.cfg.warehouse.default_base
    _ = ctx.graph()  # validates the model registry before touching the catalog
    final_name = disambiguate(name, ctx._existing_refs() - {name})
    created = 0
    for table_id in _list_tables(ctx.catalog):
        try:
            ensure_branch(ctx.catalog, table_id, final_name, base_ref)
            created += 1
        except Exception as exc:  # noqa: BLE001 — tables without snapshots are skipped
            if _show_text(json_output, quiet):
                typer.secho(f"skip {table_id}: {exc}", fg=typer.colors.YELLOW, err=True)
    key = ctx.git_branch or final_name
    ctx.state.branches[key] = BranchState(
        git_branch=ctx.git_branch or "",
        data_branch=final_name,
        base_ref=base_ref,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    ctx.store.save(ctx.state)
    _emit(
        envelope.envelope(
            "branch create",
            ok=True,
            data={"branch": final_name, "base_ref": base_ref, "tables": created},
            branch={"git": ctx.git_branch, "data": final_name},
        ),
        json_output,
    )


@branch_app.command(name="list")
@_run_guard
def branch_list(
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    ctx = Context(config_path, profile)
    branches = [
        {
            "key": k,
            "data_branch": b.data_branch,
            "base_ref": b.base_ref,
            "scope": b.scope,
            "age_days": round((time.time() - b.epoch) / 86400, 1),
        }
        for k, b in ctx.state.branches.items()
    ]
    _emit(envelope.envelope("branch list", ok=True, data={"branches": branches}), json_output)


@branch_app.command(name="show")
@_run_guard
def branch_show(
    name: str = typer.Argument(...),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Catalog refs + pin tags + local state for one branch."""
    ctx = Context(config_path, profile)
    refs: list[dict] = []
    for table_id in _list_tables(ctx.catalog):
        try:
            table = ctx.catalog.load_table(table_id)
        except Exception:  # noqa: BLE001
            continue
        for ref_name, ref in table.refs().items():
            from .catalog import snapshot_id_of

            if name in ref_name:
                refs.append(
                    {
                        "table": table_id,
                        "ref": ref_name,
                        "snapshot": snapshot_id_of(ref),
                    }
                )
    _emit(envelope.envelope("branch show", ok=True, data={"refs": refs}), json_output)


@branch_app.command()
@_run_guard
def discard(
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Drop branch refs and pin tags; refuses if promote was in progress."""
    ctx = Context(config_path, profile)
    if (ctx.reble_dir / "promote.json").exists():
        raise RebleError(
            "promote in progress — resume it or remove .reble/promote.json first"
        )
    if not yes and _show_text(json_output, quiet):
        typer.confirm(f"Discard data branch '{name}' (refs + pin tags)?", abort=True)
    dropped = 0
    for table_id in _list_tables(ctx.catalog):
        try:
            table = ctx.catalog.load_table(table_id)
            refs = dict(table.refs())
        except Exception:  # noqa: BLE001
            continue
        for ref_name in refs:
            if ref_name == name:
                drop_ref(ctx.catalog, table_id, name, "branch")
                dropped += 1
            elif ref_name.startswith(ctx.cfg.branching.tag_prefix + name + "__"):
                drop_ref(ctx.catalog, table_id, ref_name, "tag")
                dropped += 1
    for key, st in list(ctx.state.branches.items()):
        if st.data_branch == name:
            del ctx.state.branches[key]
    ctx.store.save(ctx.state)
    _emit(
        envelope.envelope("branch discard", ok=True, data={"dropped_refs": dropped}),
        json_output,
    )


@app.command()
@_run_guard
def gc(
    before_days: int = typer.Option(None, "--before", help="Days older than this expire"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Expire TTL'd branches, drop orphan pin tags. GC is a correctness command."""
    ctx = Context(config_path, profile)
    ttl = before_days if before_days is not None else ctx.cfg.branching.ttl_days
    active_tags = set()
    expired: list[str] = []
    for key, st in ctx.state.branches.items():
        active_tags.update(p.tag for p in st.pins.values())
        if (time.time() - st.epoch) / 86400 > ttl:
            expired.append(key)

    orphans = orphan_pin_tags(ctx.catalog, ctx.cfg, active_tags)
    if not dry_run:
        for key in expired:
            st = ctx.state.branches[key]
            for table_id in _list_tables(ctx.catalog):
                try:
                    if st.data_branch in ctx.catalog.load_table(table_id).refs():
                        drop_ref(ctx.catalog, table_id, st.data_branch, "branch")
                except Exception:  # noqa: BLE001
                    continue
            del ctx.state.branches[key]
        ctx.store.save(ctx.state)
        for table_id, tag in orphans:
            try:
                drop_ref(ctx.catalog, table_id, tag, "tag")
            except Exception:  # noqa: BLE001
                pass
    _emit(
        envelope.envelope(
            "gc",
            ok=True,
            data={
                "expired_branches": expired,
                "orphan_tags": [f"{t}:{g}" for t, g in orphans],
                "dry_run": dry_run,
            },
        ),
        json_output,
    )


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=str))


def _dump_yaml(data: dict) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False)


if __name__ == "__main__":  # pragma: no cover
    app()
