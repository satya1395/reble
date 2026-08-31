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
    click.echo(f"  scope ({len(m.scope)} writable): {', '.join(m.scope)}")
    click.echo(f"  pins  ({len(m.pins)} read-only tables frozen at branch creation)")


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
            changed = "n/a (no id column)" if d.changed is None else f"{d.changed:,}"
            click.echo(f"    +{d.added:,} added   -{d.removed:,} removed   "
                       f"~{changed} changed")
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


@cli.group()
def branch():
    """Create, list, switch, and delete warehouse branches."""


@branch.command("create")
@click.argument("name")
@click.option("--tables", required=True,
              help="Comma-separated tables this branch will change (namespace.table)")
def branch_create(name: str, tables: str):
    """Create a subset branch scoped to TABLES and switch to it."""
    scope = [t.strip() for t in tables.split(",") if t.strip()]
    try:
        eng = _engine()
        m = eng.create(name, scope)
    except RebleError as e:
        _fail(e)
    click.echo(f"Created branch {click.style(name, bold=True)} "
               f"(scope: {', '.join(m.scope)}; {len(m.pins)} tables pinned)")
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
