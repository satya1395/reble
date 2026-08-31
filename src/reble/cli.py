from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import click

from reble import __version__
from reble.errors import RebleError

GLYPH = "⎇"          # branch context, opens every command
CHECK = "✓"
CROSS = "✗"
WARN = "⚠"


def _engine():
    from reble.branches import BranchEngine
    from reble.config import load_config
    return BranchEngine(load_config())


def _fail(e: Exception) -> None:
    click.secho(f"{CROSS} {e}", fg="red", err=True)
    sys.exit(1)


def _br(name: str) -> str:
    return click.style(name, fg="cyan", bold=True)


def _tb(name: str) -> str:
    return click.style(name, bold=True)


def _dim(s: str) -> str:
    return click.style(s, dim=True)


def _glyph() -> str:
    return click.style(GLYPH, fg="cyan")


def _ctx(branch: str, to: str | None = None, suffix: str = "") -> None:
    line = f"{_glyph()} {_br(branch)}"
    if to:
        line += f" {_dim('→')} {_br(to)}"
    if suffix:
        line += f" {_dim(suffix)}"
    click.echo(line)


def _ok(msg: str) -> None:
    click.echo(click.style(CHECK, fg="green", bold=True) + " " + msg)


def _warn(msg: str, extra: str = "") -> None:
    click.echo(click.style(WARN, fg="yellow") + " " + msg)
    if extra:
        click.echo("  " + _dim(extra))


def _ago(ts: float) -> str:
    d = time.time() - ts
    if d < 90:
        return "just now"
    if d < 5400:
        return f"{int(d // 60)}m ago"
    if d < 129600:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


@click.group()
@click.version_option(__version__, prog_name="reble")
def cli():
    """Reble — branch your warehouse like you branch your code."""


@cli.command()
@click.argument("name")
def init(name: str):
    """Create a new reble project in directory NAME."""
    from reble.scaffold import scaffold
    try:
        path = scaffold(Path(name))
    except RebleError as e:
        _fail(e)
    _ok(f"initialized reble project in {_tb(str(path))}/")
    click.echo("  " + _dim("reble.yml") + "                    project config")
    click.echo("  " + _dim("models/demo/example.sql") + "      a model is just a SQL file")
    click.echo("  " + _dim("models/demo/example_summary.sql"))
    click.echo(f"\nNext: cd {name} && reble run")


@cli.command()
def status():
    """Show current branch, scope, and pins."""
    try:
        eng = _engine()
        m = eng.current()
    except RebleError as e:
        _fail(e)
    if m is None:
        _ctx("main")
        return
    _ctx(m.name)
    click.echo(f"  {_dim('created')}   {datetime.fromtimestamp(m.created_at):%Y-%m-%d %H:%M}"
               f" {_dim(f'· ttl {m.ttl_days}d')}")
    if m.open_scope:
        click.echo(f"  {_dim('scope')}     {_dim('open — grows when you edit models and run')}")
        if m.scope:
            click.echo(f"  {_dim('writable')}  " + ", ".join(_tb(t) for t in m.scope))
        click.echo(f"  {_dim('reads')}     {_dim('every table frozen as of the branch epoch')}")
    else:
        click.echo(f"  {_dim('writable')}  " + ", ".join(_tb(t) for t in m.scope))
        pins = sorted(m.pins)
        if pins:
            shown = ", ".join(pins[:2])
            more = f" {_dim(f'+{len(pins) - 2} more,')}" if len(pins) > 2 else ""
            click.echo(f"  {_dim('pinned')}    {shown}{more} {_dim('frozen at epoch')}")
        else:
            click.echo(f"  {_dim('pinned')}    {_dim('(none)')}")


@cli.command()
@click.argument("table")
@click.argument("file", type=click.Path(exists=True))
@click.option("--overwrite", is_flag=True,
              help="Replace the table's contents instead of appending")
def load(table: str, file: str, overwrite: bool):
    """Load a CSV or Parquet FILE into TABLE (e.g. raw.orders seeds/orders.csv).

    Writes are routed and guarded by the current branch like any other write.
    """
    import duckdb
    try:
        eng = _engine()
        ctx_name = eng.state.current_branch()
        con = duckdb.connect()
        reader = ("read_parquet" if file.endswith((".parquet", ".pq"))
                  else "read_csv_auto")
        arrow = con.execute(f"SELECT * FROM {reader}(?)", [file]).to_arrow_table()
        con.close()
        eng.write(table, arrow, mode="overwrite" if overwrite else "append")
    except RebleError as e:
        _fail(e)
    _ctx(ctx_name)
    mode = "overwrite" if overwrite else "append"
    _ok(f"{arrow.num_rows:,} rows {_dim('→')} {_tb(table)} {_dim(f'({mode})')}")


@cli.command()
@click.option("--force", is_flag=True,
              help="Rerun every model regardless of change detection "
                   "(e.g. to refresh outputs after new raw data arrived)")
def run(force: bool):
    """Run changed models on the current branch, publish to Iceberg."""
    from reble.config import load_config
    from reble.runner import run as _run
    try:
        cfg = load_config()
        from reble.branches import BranchEngine
        eng = BranchEngine(cfg)
        t0 = time.time()
        res = _run(cfg, eng, force=force)
        took = time.time() - t0
    except RebleError as e:
        _fail(e)
    _ctx(res.environment)
    on_branch = res.environment != "main"
    if res.changed:
        click.echo(f"  {_dim('changed')}    " + ", ".join(_tb(t) for t in res.changed))
    if res.published:
        dest = _dim("→ branch ref" if on_branch else "→ main")
        click.echo(f"  {_dim('published')}  "
                   + ", ".join(_tb(t) for t in res.published) + f" {dest}")
    for tbl in res.guard_skipped:
        _warn(f"{_tb(tbl)} changed but is outside this branch's scope "
              + _dim("— not published"),
              "add it to the scope, or rethink the change")
    n = len(res.published)
    if n:
        _ok(f"{n} model{'s' if n != 1 else ''} in {took:.1f}s")
    else:
        _ok(_dim("up to date — nothing to run"))


@cli.command("query")
@click.argument("sql")
def query_cmd(sql: str):
    """Run ad-hoc SQL against the warehouse as the current branch sees it.

    Scoped tables read your branch refs; everything else reads its pinned or
    epoch snapshot. On main you see main. Example:

        reble query "SELECT * FROM demo.example LIMIT 20"
    """
    from reble.config import load_config
    from reble.runner import query as _query
    try:
        cfg = load_config()
        from reble.branches import BranchEngine
        eng = BranchEngine(cfg)
        ctx_name = eng.state.current_branch()
        con, rel = _query(cfg, eng, sql)
    except RebleError as e:
        _fail(e)
    _ctx(ctx_name)
    try:
        rel.show(max_rows=40)
    except TypeError:
        rel.show()
    con.close()


@cli.command()
def diff():
    """Show what the current branch changes, per scoped table."""
    from reble.diffing import diff_branch
    try:
        eng = _engine()
        diffs = diff_branch(eng)
        branch_name = eng.state.current_branch()
    except RebleError as e:
        _fail(e)
    if not diffs:
        click.echo("No written tables on this branch yet — run `reble run` first.")
        return
    _ctx(branch_name, suffix="vs base")
    click.echo()
    for d in diffs:
        if d.kind == "profile":
            click.echo(f"  {_tb(d.table)}  {_dim('new table — profile')}")
            cols = " · ".join(
                f"{n} {t}" + (" " + click.style(f"{nu} nulls", fg="yellow") if nu else "")
                for n, t, nu in d.profile_columns)
            click.echo(f"    {_dim('rows')} {d.rows_branch:,}   {_dim('cols')} {cols}")
        else:
            key = _dim(f" · key {d.key}") if d.key else ""
            click.echo(f"  {_tb(d.table)}  "
                       + _dim(f"{d.rows_main:,} → {d.rows_branch:,} rows") + key)
            for c in d.schema_added:
                click.echo("    " + click.style(f"+ column {c}", fg="green"))
            for c in d.schema_removed:
                click.echo("    " + click.style(f"− column {c}", fg="red"))
            parts = []
            if d.added:
                parts.append(click.style(f"+{d.added:,} added", fg="green"))
            if d.removed:
                parts.append(click.style(f"−{d.removed:,} removed", fg="red"))
            if d.changed:
                parts.append(click.style(f"~{d.changed:,} changed", fg="yellow"))
            if not parts:
                parts.append(_dim("no row changes"))
            note = ("   " + _dim("no unique key: changes count as +/− pairs")
                    if d.key is None and (d.added or d.removed) else "")
            click.echo("    " + "  ".join(parts) + note)
        click.echo()


@cli.command()
def promote():
    """Fast-forward main to this branch's results and delete the branch."""
    try:
        eng = _engine()
        res = eng.promote()
    except RebleError as e:
        _fail(e)
    _ctx(res["branch"], to="main")
    for t in res["promoted"]:
        click.echo("  " + click.style(CHECK, fg="green", bold=True) + f" {_tb(t)}")
    if not res["promoted"]:
        click.echo("  " + _dim("(no written tables — branch deleted)"))
    for t in res["stale_pins"]:
        _warn(f"pinned input {_tb(t)} advanced on main while you worked "
              + _dim("— results used the older snapshot"))
    _ok(f"promoted {_dim('· branch deleted · on')} {_br('main')}")


@cli.command()
def gc():
    """Delete branches past their TTL (drops refs, releases pins)."""
    try:
        expired = _engine().gc()
    except RebleError as e:
        _fail(e)
    if expired:
        for name in expired:
            _ok(f"deleted expired branch {_br(name)}")
    else:
        _ok(_dim("nothing to collect — no branches past their TTL"))


@cli.group()
def branch():
    """Create, list, switch, and delete warehouse branches."""


@branch.command("create")
@click.argument("name")
@click.option("--tables", default=None,
              help="Comma-separated tables this branch will change "
                   "(default: inferred from your edited models via SQLGlot)")
@click.option("--pin-all", is_flag=True,
              help="Pin every non-scoped table instead of just the lineage-derived "
                   "upstream inputs (use when external writers touch tables SQLGlot "
                   "can't see)")
def branch_create(name: str, tables: str | None, pin_all: bool):
    """Create a subset branch and switch to it.

    With no --tables, the scope is inferred from what you changed: edit your
    models first, then branch. On a clean tree you get a branch-first open
    branch whose scope grows at run time.
    """
    from reble.config import load_config
    from reble.models import upstream_closure
    from reble.runner import analyze_project
    try:
        cfg = load_config()
        eng = _engine()
        prev = eng.state.current_branch()
        inferred = False
        if tables:
            scope = [t.strip() for t in tables.split(",") if t.strip()]
        else:
            scope, models = analyze_project(cfg, eng)
            inferred = True
            if not scope:
                eng.create(name, [], open_scope=True)
                _ctx(prev, to=name)
                click.echo(f"  {_dim('scope')}  "
                           + _dim("open — grows when you edit models and run"))
                click.echo(f"  {_dim('reads')}  "
                           + _dim("every table frozen as of now (the branch epoch)"))
                _ok(f"switched to {_br(name)}")
                return
        pin_tables = None
        if not pin_all:
            if not inferred:
                _, models = analyze_project(cfg, eng)
            pin_tables = upstream_closure(scope, models)   # None if uninferrable
        m = eng.create(name, scope, pin_tables=pin_tables)
    except RebleError as e:
        _fail(e)
    _ctx(prev, to=name)
    how = "inferred from your changes" if inferred else "explicit"
    click.echo(f"  {_dim('scope')}  " + ", ".join(_tb(t) for t in m.scope)
               + f"  {_dim(how)}")
    pins = sorted(m.pins)
    if pins:
        shown = ", ".join(pins[:3]) + (f" {_dim(f'+{len(pins) - 3}')}" if len(pins) > 3 else "")
        click.echo(f"  {_dim('pins')}   {shown}  {_dim('upstream inputs, frozen now')}")
    else:
        click.echo(f"  {_dim('pins')}   {_dim('(none)')}")
    for other, t in getattr(m, "overlaps", []):
        _warn(f"{_tb(t)} is also scoped by branch {_br(other)} "
              + _dim("— second promote will require a rebase"))
    _ok(f"switched to {_br(name)}")


@branch.command("list")
def branch_list():
    """List branches."""
    try:
        eng = _engine()
        current = eng.state.current_branch()
        branches = eng.state.list()
    except RebleError as e:
        _fail(e)
    dot = click.style("●", fg="green")
    mark = dot if current == "main" else " "
    click.echo(f"{mark} {_glyph()} main")
    for m in branches:
        mark = dot if m.name == current else " "
        if m.open_scope:
            info = _dim(f"open scope · created {_ago(m.created_at)}")
        else:
            n = len(m.scope)
            info = _dim(f"{n} table{'s' if n != 1 else ''} · created {_ago(m.created_at)}")
        click.echo(f"{mark} {_glyph()} {_br(m.name)}   {info}")


@branch.command("switch")
@click.argument("name")
def branch_switch(name: str):
    """Switch to branch NAME (or 'main')."""
    try:
        _engine().switch(name)
    except RebleError as e:
        _fail(e)
    _ok(f"on {_br(name)}")


@branch.command("delete")
@click.argument("name")
def branch_delete(name: str):
    """Delete branch NAME: drop its refs, release its pins."""
    try:
        _engine().delete(name)
    except RebleError as e:
        _fail(e)
    _ok(f"deleted branch {_br(name)}")


if __name__ == "__main__":
    cli()
