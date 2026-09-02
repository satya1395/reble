"""Reble MCP server — the core verbs as agent tools (design notes rules 5.2, 5.5).

The agent has no special powers: every tool is a thin wrapper over
`core.Reble`, returning the same JSON envelopes the CLI emits. Spec exit
codes surface as structured error objects (``error.code``) instead of
process exits, so agents can branch on them (3 = drift, 4 = promote-blocked,
7 = missing diff key, ...).

Transport: stdio. The MCP host launches `reble-mcp` (or `reble mcp`) and
points it at a project via the `REBLE_PROJECT_DIR` environment variable
(directory containing reble.yml); `REBLE_PROFILE` is optional.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    raise ModuleNotFoundError(
        "The MCP extra is not installed. Run: pip install 'reble[mcp]'"
    ) from exc

from . import __version__
from .core import Reble
from .errors import RebleError

server = MCPServer(
    name="reble",
    version=__version__,
    instructions=(
        "Transactional data changes on an Iceberg lakehouse: scope, pin, run, "
        "diff, promote-or-discard. Never a three-way merge. Workflow: call "
        "reble_run (it returns a change-set id — pass it to every later call), "
        "inspect reble_diff, then reble_promote or reble_branch_discard. "
        "Every result carries ok/warnings; errors carry error.code "
        "(spec exit codes: 2 config, 3 drift, 4 promote-blocked, 5 empty scope, "
        "6 lineage, 7 missing diff key)."
    ),
)


def _core() -> Reble:
    root = os.environ.get("REBLE_PROJECT_DIR")
    return Reble(Path(root).resolve() if root else Path.cwd(), os.environ.get("REBLE_PROFILE") or None)


def _error(exc: RebleError) -> dict:
    """Spec exit code → structured error object; keep any partial payload."""
    result = dict(exc.payload or {})
    result["ok"] = False
    result["error"] = {"code": exc.exit_code, "message": str(exc)}
    result.setdefault("errors", [str(exc)])
    return result


def _generated_changeset() -> str:
    return f"mcp-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------- run


@server.tool(
    name="reble_run",
    annotations=ToolAnnotations(idempotent_hint=True),
)
def reble_run(
    models: list[str] | None = None,
    depth: int | None = None,
    dry_run: bool = False,
    change_set: str | None = None,
    branch: str | None = None,
) -> dict:
    """Materialize a scoped change on an isolated Iceberg branch.

    Scope = edited models ∪ downstream closure; upstream inputs are pinned
    with Iceberg tags so the run is deterministic. Idempotent: unchanged
    models are skipped. If change_set is omitted, one is generated and
    returned as `changeset` — pass it to reble_status/reble_diff/reble_promote.
    On a fresh change-set with no prior run, pass `models` explicitly (fresh
    branches start with empty scope). Set dry_run=True to preview scope,
    pins, and full-refreshs without writing.
    """
    core = _core()
    generated = None
    if not change_set:
        generated = _generated_changeset()
    try:
        env = core.run(
            models=models,
            depth=depth,
            dry_run=dry_run,
            change_set=change_set or generated,
            branch=branch,
        )
    except RebleError as exc:
        return _error(exc)
    if generated:
        env["changeset"] = generated
    return env


# --------------------------------------------------------------------- diff


@server.tool(
    name="reble_diff",
    annotations=ToolAnnotations(read_only_hint=True),
)
def reble_diff(
    tables: list[str] | None = None,
    against: str = "base",
    schema_only: bool = False,
    rows: int | None = None,
    full: bool = False,
    change_set: str | None = None,
    branch: str | None = None,
) -> dict:
    """Row-level + schema diff of the branch's scope tables.

    Per table: +added / -removed / ~changed counts, key columns, sample rows,
    schema delta. against="base" diffs the branch point; against="main" is
    the advisory "what would promote do" preview. Diff keys come from the
    model's `key:` header or reble.yml; keyless tables fall back to a
    full-row hash compare (error.code 7 when on_missing_key=error).
    """
    try:
        return _core().diff(
            tables=tables,
            against=against,
            schema_only=schema_only,
            rows=rows,
            full=full,
            change_set=change_set,
            branch=branch,
        )
    except RebleError as exc:
        return _error(exc)


# ------------------------------------------------------------------- status


@server.tool(
    name="reble_status",
    annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True),
)
def reble_status(change_set: str | None = None, branch: str | None = None) -> dict:
    """Where was I? Un-run edits, branch scope, drifted pins, age/expiry.

    Read-only and CI-safe. ok=false with error.code 3 means drift: an input
    pin or a scope table's base no longer equals current main — the next
    promote will force a scoped re-run and emit a fresh promote-time diff.
    """
    try:
        return _core().status(change_set=change_set, branch=branch)
    except RebleError as exc:
        return _error(exc)


# ------------------------------------------------------------------ promote


@server.tool(
    name="reble_promote",
    annotations=ToolAnnotations(destructive_hint=True),
)
def reble_promote(
    ff_only: bool = False,
    dry_run: bool = False,
    change_set: str | None = None,
    branch: str | None = None,
) -> dict:
    """Accept the change: per-table fast-forwards of main to the branch heads.

    Legal only when every pin still equals current main; otherwise Reble
    re-pins, re-runs the scope, emits the authoritative promote-time diff,
    then fast-forwards. ff_only=True refuses under drift instead
    (error.code 4). There is no three-way merge, ever: promote or discard.
    """
    try:
        return _core().promote(ff_only=ff_only, dry_run=dry_run, change_set=change_set, branch=branch)
    except RebleError as exc:
        return _error(exc)


# ------------------------------------------------------------------ branch


@server.tool(name="reble_branch_create")
def reble_branch_create(name: str, from_ref: str | None = None, change_set: str | None = None) -> dict:
    """Create an explicit zero-copy data branch (refs only, no data copied)."""
    try:
        return _core().branch_create(name, from_ref, change_set)
    except RebleError as exc:
        return _error(exc)


@server.tool(
    name="reble_branch_list",
    annotations=ToolAnnotations(read_only_hint=True),
)
def reble_branch_list() -> dict:
    """List change-sets with their data branches, key source, scope, and age."""
    try:
        return _core().branch_list()
    except RebleError as exc:
        return _error(exc)


@server.tool(
    name="reble_branch_show",
    annotations=ToolAnnotations(read_only_hint=True),
)
def reble_branch_show(name: str) -> dict:
    """Show catalog refs (branches + pin tags) matching a branch name."""
    try:
        return _core().branch_show(name)
    except RebleError as exc:
        return _error(exc)


@server.tool(
    name="reble_branch_discard",
    annotations=ToolAnnotations(destructive_hint=True),
)
def reble_branch_discard(name: str) -> dict:
    """Discard a data branch: drop its refs and pin tags. Refuses while a
    promote is in progress. This is the only alternative to promote."""
    try:
        return _core().branch_discard(name)
    except RebleError as exc:
        return _error(exc)


# ---------------------------------------------------------------------- gc


@server.tool(
    name="reble_gc",
    annotations=ToolAnnotations(destructive_hint=True),
)
def reble_gc(dry_run: bool = False, before_days: int | None = None) -> dict:
    """Expire TTL'd branches and drop orphan pin tags.

    Correctness command, not hygiene: orphan pin tags block snapshot
    expiration on production tables. dry_run=True lists what would go.
    """
    try:
        return _core().gc(before_days, dry_run)
    except RebleError as exc:
        return _error(exc)


def main() -> None:
    """Console-script entry point (`reble-mcp`)."""
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
