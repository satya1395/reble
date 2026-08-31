from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import click

from reble import __version__
from reble.errors import RebleError


def _engine():
    from reble.branches import BranchEngine
    from reble.config import load_config
    return BranchEngine(load_config())


def _fail(e: Exception) -> None:
    click.secho(f"error: {e}", fg="red", err=True)
    sys.exit(1)


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
    click.echo(f"Initialized reble project in {path}/")
    click.echo("  reble.yml          project config")
    click.echo("  config.yaml        SQLMesh config")
    click.echo("  models/example.sql starter model")
    click.echo(f"\nNext: cd {name} && reble branch list")


@cli.command()
def status():
    """Show current branch, scope, and pins."""
    try:
        eng = _engine()
        m = eng.current()
    except RebleError as e:
        _fail(e)
    if m is None:
        click.echo("On main")
        return
    click.echo(f"On branch {click.style(m.name, bold=True)}")
    click.echo(f"  created {datetime.fromtimestamp(m.created_at):%Y-%m-%d %H:%M} "
               f"(ttl {m.ttl_days}d)")
    if m.open_scope:
        click.echo(f"  scope (open, grows on run): {', '.join(m.scope) or '(none yet)'}")
        click.echo("  reads: frozen as of the branch epoch")
    else:
        click.echo(f"  scope ({len(m.scope)} writable): {', '.join(m.scope)}")
        click.echo(f"  pins  ({len(m.pins)} upstream inputs frozen at the epoch)")


@cli.command()
def run():
    """Run models on the current branch (SQLMesh plan/apply + publish to Iceberg)."""
    from reble.config import load_config
    from reble.runner import run as _run
    try:
        cfg = load_config()
        from reble.branches import BranchEngine
        res = _run(cfg, BranchEngine(cfg))
    except RebleError as e:
        _fail(e)
    click.echo(f"Environment: {res.environment}")
    if res.mirrored:
        click.echo(f"  mirrored inputs : {', '.join(res.mirrored)}")
    click.echo(f"  models changed  : {', '.join(res.changed) or '(none)'}")
    click.echo(f"  published       : {', '.join(res.published) or '(none)'}")
    for tbl in res.guard_skipped:
        click.secho(
            f"  guard: {tbl} changed but is NOT in this branch's scope — "
            "not published. Add it to the scope or rethink the change.",
            fg="yellow",
        )


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
    click.echo(f"Branch {click.style(branch_name, bold=True)} vs base:\n")
    for d in diffs:
        if d.kind == "profile":
            click.secho(f"  {d.table}  (new table — profile)", bold=True)
            click.echo(f"    rows: {d.rows_branch:,}")
            for name, typ, nulls in d.profile_columns:
                nul = f", {nulls:,} nulls" if nulls else ""
                click.echo(f"    {name}: {typ}{nul}")
        else:
            click.secho(f"  {d.table}", bold=True)
            for c in d.schema_added:
                click.secho(f"    schema: + column {c}", fg="green")
            for c in d.schema_removed:
                click.secho(f"    schema: - column {c}", fg="red")
            click.echo(f"    rows: {d.rows_main:,} -> {d.rows_branch:,}")
            line = f"    +{d.added:,} added   -{d.removed:,} removed"
            if d.changed is not None:
                line += f"   ~{d.changed:,} changed"
            else:
                line += "   (no id column: changed rows count as +/- pairs)"
            click.echo(line)
        click.echo()


@cli.command()
def promote():
    """Fast-forward main to this branch's results and delete the branch."""
    try:
        res = _engine().promote()
    except RebleError as e:
        _fail(e)
    click.echo(f"Promoted branch {click.style(res['branch'], bold=True)} to main:")
    for t in res["promoted"]:
        click.echo(f"  {t}")
    if not res["promoted"]:
        click.echo("  (no written tables — branch deleted)")
    for t in res["stale_pins"]:
        click.secho(
            f"  note: pinned input {t} advanced on main while you worked — "
            "your results were computed from the older snapshot.", fg="yellow")
    click.echo("Back on main")


@cli.command()
def gc():
    """Delete branches past their TTL (drops refs, releases pins)."""
    try:
        expired = _engine().gc()
    except RebleError as e:
        _fail(e)
    if expired:
        for name in expired:
            click.echo(f"Deleted expired branch {name}")
    else:
        click.echo("Nothing to collect — no branches past their TTL.")


@cli.group()
def branch():
    """Create, list, switch, and delete warehouse branches."""


@branch.command("create")
@click.argument("name")
@click.option("--tables", default=None,
              help="Comma-separated tables this branch will change "
                   "(default: inferred from your edited models via SQLMesh)")
@click.option("--pin-all", is_flag=True,
              help="Pin every non-scoped table instead of just the lineage-derived "
                   "upstream inputs (use when external writers touch tables SQLMesh "
                   "doesn't know about)")
def branch_create(name: str, tables: str | None, pin_all: bool):
    """Create a subset branch and switch to it.

    With no --tables, the scope is inferred from what you changed: edit your
    models first, then branch — SQLMesh reports the changed models and their
    downstream cascade, and their upstream inputs become the pins.
    """
    from reble.config import load_config
    from reble.runner import analyze_project, upstream_closure
    try:
        cfg = load_config()
        eng = _engine()
        inferred = False
        if tables:
            scope = [t.strip() for t in tables.split(",") if t.strip()]
        else:
            scope, deps = analyze_project(cfg)
            inferred = True
            if not scope:
                # branch-first, git-style: open scope + frozen epoch
                m = eng.create(name, [], open_scope=True)
                click.echo(f"Created branch {click.style(name, bold=True)} "
                           "(branch-first: no changes yet)")
                click.echo("  scope: open — grows automatically when you edit "
                           "models and `reble run`")
                click.echo("  reads: every table frozen as of this moment "
                           "(the branch epoch)")
                click.echo(f"Switched to {name}")
                return
        pin_tables = None
        if not pin_all:
            if not inferred:
                _, deps = analyze_project(cfg)
            pin_tables = upstream_closure(scope, deps)   # None if uninferrable
        m = eng.create(name, scope, pin_tables=pin_tables)
    except RebleError as e:
        _fail(e)
    how = "inferred from your changes" if inferred else "explicit"
    click.echo(f"Created branch {click.style(name, bold=True)}")
    click.echo(f"  scope ({how}): {', '.join(m.scope)}")
    pins = sorted(m.pins)
    shown = ", ".join(pins[:6]) + (" …" if len(pins) > 6 else "")
    click.echo(f"  pins  ({len(pins)}): {shown or '(none)'}")
    for other, t in getattr(m, "overlaps", []):
        click.secho(f"  warning: {t} is also scoped by branch {other!r} — "
                    "second promote will require a rebase", fg="yellow")
    click.echo(f"Switched to {name}")


@branch.command("list")
def branch_list():
    """List branches."""
    try:
        eng = _engine()
        current = eng.state.current_branch()
        branches = eng.state.list()
    except RebleError as e:
        _fail(e)
    marker = "*" if current == "main" else " "
    click.echo(f"{marker} main")
    for m in branches:
        marker = "*" if m.name == current else " "
        click.echo(f"{marker} {m.name}  (scope: {', '.join(m.scope)})")


@branch.command("switch")
@click.argument("name")
def branch_switch(name: str):
    """Switch to branch NAME (or 'main')."""
    try:
        _engine().switch(name)
    except RebleError as e:
        _fail(e)
    click.echo(f"Switched to {name}")


@branch.command("delete")
@click.argument("name")
def branch_delete(name: str):
    """Delete branch NAME: drop its refs, release its pins."""
    try:
        _engine().delete(name)
    except RebleError as e:
        _fail(e)
    click.echo(f"Deleted branch {name}")


if __name__ == "__main__":
    cli()
