"""Reble CLI (spec sections 4–6) — a thin adapter over the headless core.

Global flags: --config PATH, --profile NAME, --json, --no-color, --quiet.
Exit codes are normative (spec section 6) and carried by RebleError.

Every command parses flags, calls the matching `core.Reble` verb, and renders
the envelope the verb returns. The MCP server is the other adapter.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import typer

from . import __version__
from .catalog import load_catalog
from .config import ConfigLoader, assert_no_secrets
from .core import Reble
from .errors import EXIT_ERROR, EXIT_OK, RebleError
from .events import ndjson_emitter

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
            raise  # deliberate exits pass through
        except Exception as exc:
            typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_ERROR) from exc

    return wrapper


def _print_version(value: bool):
    if value:
        typer.echo(f"reble {__version__}")
        raise typer.Exit(EXIT_OK)


# Global flags set by the root callback — spec: "Global flags on every
# command". A command-local flag ORs with the global one.
_FLAGS = {"json": False, "quiet": False, "ndjson": False}


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
        if _FLAGS.get("ndjson"):
            typer.echo(json.dumps(env, default=str))  # one line: NDJSON-compatible
        else:
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


def _invoke(verb, json_output: bool, *args, **kwargs) -> dict:
    """Call a core verb; failures that carry an envelope still emit it."""
    try:
        return verb(*args, **kwargs)
    except RebleError as exc:
        if exc.payload:
            _emit(exc.payload, json_output)
        raise


def _core(config_path: Path | None, profile: str | None) -> Reble:
    root = (config_path.parent if config_path else Path.cwd()).resolve()
    return Reble(root, profile)


def _print_preflight(preflight: dict, quiet: bool) -> None:
    if quiet:
        return
    for key, value in preflight.items():
        if not isinstance(value, (tuple, list)):
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


def _dump_yaml(data: dict) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False)


# ---------------------------------------------------------------------- init


@app.command()
@_run_guard
def init(
    catalog_type: str = typer.Option(
        "rest", "--catalog", help="glue|polaris|nessie|hive|rest|reble|sql"
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
    from . import envelope

    root = (config_path.parent if config_path else Path.cwd()).resolve()
    loader = ConfigLoader(root)

    detected: dict[str, str] = {}
    assumptions: list[str] = []
    machine_local: list[str] = []
    models_dir = root / "models"
    if models_dir.is_dir():
        count = len(list(models_dir.rglob("*.sql")))
        detected["models"] = f"{count} SQL models under {models_dir.relative_to(root)}/"
        if count == 0:
            assumptions.append("models/ exists but contains no .sql files")
    else:
        assumptions.append("no models/ directory — set lineage.models_path in reble.yml")

    if loader.config_path.exists() and not yes:
        raise RebleError(f"{loader.config_path} already exists — pass --yes to overwrite", 2)

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
    from .gitinfo import repo_root

    if repo_root(root) is None:
        raw["branching"] = {"git_sync": False}
        assumptions.append(
            "no git repository — wrote git_sync: false; work is keyed by the "
            "'local' change-set (pass --change-set or git init to change that)"
        )
    if catalog_type == "sql":
        # working local defaults: sqlite catalog + file warehouse
        raw["warehouse"]["catalog"].update(
            {
                "uri": f"sqlite:///{root / 'catalog.db'}",
                "warehouse": f"file://{root / 'warehouse'}",
            }
        )
        (root / "warehouse").mkdir(exist_ok=True)
        machine_local += ["catalog.db", "warehouse/"]
        assumptions.append("sql catalog: local sqlite + ./warehouse (dev defaults)")
    elif catalog_type == "in-memory":
        raw["warehouse"]["catalog"].update({"warehouse": f"file://{root / 'warehouse'}"})
        (root / "warehouse").mkdir(exist_ok=True)
        assumptions.append("in-memory catalog does not persist — per-process only")
    if namespace:
        raw["warehouse"]["namespace"] = namespace
    assert_no_secrets(raw)
    loader.config_path.write_text(_dump_yaml(raw))

    # .reble/ is machine-local (spec section 2); init adds the gitignore
    # entries (plus sql-catalog artifacts when we generate them)
    loader.reble_dir.mkdir(exist_ok=True)
    gitignore = root / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    for entry in [".reble/", *machine_local]:
        if entry not in lines:
            lines.append(entry)
    gitignore.write_text("\n".join([*lines, ""]) if lines else "")

    # Probe catalog connectivity through the config we just wrote — also
    # validates it. Exit 2 if unreachable (spec section 4).
    probe = load_catalog(loader.load().warehouse.catalog)
    from .core import list_tables

    _ = list_tables(probe)

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


# ----------------------------------------------------------------------- run


@app.command()
@_run_guard
def run(
    models: str | None = typer.Option(None, "--models", help="Comma-separated model names"),
    depth: int | None = typer.Option(None, "--depth", help="Cap downstream cascade"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    engine_name: str | None = typer.Option(None, "--engine", help="duckdb|spark"),
    change_set: str | None = typer.Option(
        None, "--change-set", help="Change-set id (primary state key; overrides git derivation)"
    ),
    branch: str | None = typer.Option(
        None, "--branch", help="Explicit data branch (resume an existing branch under this change-set)"
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Data-driven scope: rebuild models whose upstream snapshots moved"
    ),
    events: bool = typer.Option(
        False, "--events", help="Stream NDJSON run events on stdout (implies machine mode)"
    ),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Resolve scope, create/update the data branch, pin inputs, execute."""
    if events:
        _FLAGS["json"] = True  # --events implies machine mode
        _FLAGS["ndjson"] = True  # envelope prints as one line: pure NDJSON stream
    core = _core(config_path, profile)
    env = _invoke(
        core.run,
        json_output,
        models=models,
        depth=depth,
        dry_run=dry_run,
        engine=engine_name,
        change_set=change_set,
        branch=branch,
        refresh=refresh,
        on_event=ndjson_emitter("run") if events else None,
    )
    if _show_text(json_output, quiet):
        if "preflight" in env["data"]:
            _print_preflight(env["data"]["preflight"], quiet)
        if not dry_run and "results" in env["data"]:
            for result in env["data"]["results"]:
                line = f"{result['model']}: {result['status']}"
                if result["status"] == "ran":
                    line += f" ({result['rows_written']} rows, {result['duration_ms']}ms)"
                if result["error"]:
                    line += f" — {result['error']}"
                typer.echo(line)
            for model in env["data"]["scope"]["incremental_full_refresh"]:
                typer.secho(
                    f"full-refresh: {model} (incremental models always full-refresh in branches)",
                    fg=typer.colors.YELLOW,
                )
    _emit(env, json_output)


# ---------------------------------------------------------------------- diff


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
    change_set: str | None = typer.Option(None, "--change-set", help="Change-set id"),
    branch: str | None = typer.Option(None, "--branch", help="Explicit data branch"),
    events: bool = typer.Option(
        False, "--events", help="Stream NDJSON diff events on stdout (implies machine mode)"
    ),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Row-level + schema diff of scope tables (exit 7 if key required and missing)."""
    if events:
        _FLAGS["json"] = True
        _FLAGS["ndjson"] = True
    core = _core(config_path, profile)
    env = _invoke(
        core.diff,
        json_output,
        tables=tables,
        against=against,
        schema_only=schema_only,
        rows=rows,
        full=full,
        change_set=change_set,
        branch=branch,
        on_event=ndjson_emitter("diff") if events else None,
    )
    _emit(env, json_output)


# ------------------------------------------------------------------ estimate


@app.command()
@_run_guard
def estimate(
    models: str | None = typer.Option(None, "--models", help="Comma-separated model names"),
    depth: int | None = typer.Option(None, "--depth", help="Cap downstream cascade"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Data-driven scope: models whose upstream snapshots moved"
    ),
    change_set: str | None = typer.Option(None, "--change-set", help="Change-set id"),
    branch: str | None = typer.Option(None, "--branch", help="Explicit data branch"),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Rough cost estimate before running: models to run, rows and bytes to read.

    Local and honest about being rough — from Iceberg snapshot summaries
    only, nothing scanned. Accurate estimation is deliberately not a goal.
    """
    core = _core(config_path, profile)
    env = _invoke(
        core.estimate,
        json_output,
        models=models,
        depth=depth,
        change_set=change_set,
        branch=branch,
        refresh=refresh,
    )
    if _show_text(json_output, quiet) and "tables" in env["data"]:
        data = env["data"]
        typer.echo(
            f"models to run: {data['models']}   "
            f"est read: {data['est_bytes_read']:,} bytes, "
            f"{data['est_input_rows']:,} input rows"
        )
        for t in data["tables"]:
            typer.echo(
                f"  {t['table']} ({t['role']}): {t['records']:,} rows, {t['bytes']:,} bytes"
            )
        typer.secho("rough: from snapshot summaries only", fg=typer.colors.YELLOW)
    _emit(env, json_output)


# -------------------------------------------------------------------- status


@app.command()
@_run_guard
def status(
    change_set: str | None = typer.Option(None, "--change-set", help="Change-set id"),
    branch: str | None = typer.Option(None, "--branch", help="Explicit data branch"),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """The 'where was I?' answer. Read-only, CI-safe. Exit 3 on drift."""
    core = _core(config_path, profile)
    env = _invoke(
        core.status, json_output, change_set=change_set, branch=branch
    )
    _emit(env, json_output)


# ------------------------------------------------------------------- promote


@app.command(name="promote")
@_run_guard
def promote_cmd(
    yes: bool = typer.Option(False, "--yes", "-y"),
    ff_only: bool = typer.Option(False, "--ff-only"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    change_set: str | None = typer.Option(None, "--change-set", help="Change-set id"),
    branch: str | None = typer.Option(None, "--branch", help="Explicit data branch"),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Fast-forward promote — only when pinned bases still equal current main."""
    core = _core(config_path, profile)
    env = _invoke(
        core.promote,
        json_output,
        ff_only=ff_only,
        dry_run=dry_run,
        change_set=change_set,
        branch=branch,
    )
    if _show_text(json_output, quiet) and "results" in env["data"]:
        typer.echo("promote complete (per-table fast-forwards; no merge, ever)")
        for table_id, r in env["data"]["results"].items():
            line = f"  {table_id}: {r.get('status')}"
            if r.get("reason"):
                line += f": {r['reason']}"
            typer.echo(line)
    _emit(env, json_output)


# ------------------------------------------------------------------- branch


@branch_app.command(name="create")
@_run_guard
def branch_create(
    name: str = typer.Argument(..., help="Explicit data branch name"),
    from_ref: str = typer.Option(
        None, "--from", help="Ref to fork from (default: warehouse default_base)"
    ),
    change_set: str | None = typer.Option(
        None, "--change-set", help="Change-set id to register for this branch"
    ),
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    """Explicit branch (required when git_sync: false). Zero-copy refs."""
    core = _core(config_path, profile)
    env = _invoke(core.branch_create, json_output, name, from_ref, change_set)
    _emit(env, json_output)


@branch_app.command(name="list")
@_run_guard
def branch_list(
    profile: str | None = typer.Option(None, "--profile"),
    json_output: bool = typer.Option(False, "--json"),
    quiet: bool = typer.Option(False, "--quiet"),
    config_path: Path | None = typer.Option(None, "--config"),
):
    core = _core(config_path, profile)
    _emit(_invoke(core.branch_list, json_output), json_output)


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
    core = _core(config_path, profile)
    _emit(_invoke(core.branch_show, json_output, name), json_output)


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
    if not yes and _show_text(json_output, quiet):
        typer.confirm(f"Discard data branch '{name}' (refs + pin tags)?", abort=True)
    core = _core(config_path, profile)
    _emit(_invoke(core.branch_discard, json_output, name), json_output)


# ------------------------------------------------------------------------ gc


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
    core = _core(config_path, profile)
    _emit(_invoke(core.gc, json_output, before_days, dry_run), json_output)


@app.command()
def mcp():
    """Run the MCP server (stdio). Agent tools over the core verbs.

    Equivalent to the `reble-mcp` console script; configure the host with
    REBLE_PROJECT_DIR (project root containing reble.yml) and optional
    REBLE_PROFILE. Requires the `mcp` extra: pip install 'reble[mcp]'.
    """
    from .mcp_server import main as mcp_main

    mcp_main()


if __name__ == "__main__":  # pragma: no cover
    app()
