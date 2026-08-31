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
