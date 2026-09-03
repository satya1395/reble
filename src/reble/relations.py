"""Relation naming and branch-aware reads, shared by runner and engines."""

from __future__ import annotations

import re

from .config import Config


def table_for_model(cfg: Config, model_name: str) -> str:
    """Model name → Iceberg table identifier (namespace-qualified if configured)."""
    ns = cfg.warehouse.namespace
    return f"{ns}.{model_name}" if ns else model_name


def relation_id(cfg: Config, ref: str) -> str:
    """Any table reference → Iceberg identifier.

    Already-qualified refs (dotted) pass through; bare refs get the warehouse
    namespace. Applies to upstream inputs as well as models.
    """
    if "." in ref:
        return ref
    return table_for_model(cfg, ref)


def tag_name(cfg: Config, branch: str, table: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", table)
    return f"{cfg.branching.tag_prefix}{branch}__{safe}"


def resolve_input_snapshot(
    table, branch: str, base_ref: str, pin_tag: str | None, in_scope: bool
) -> int | None:
    """Which snapshot an input read resolves to (invariant 4).

    Order: branch ref → pin tag → base ref → None (= current snapshot).
    The tag is the source of truth for pinned reads; the snapshot id is an
    in-process detail of asking the catalog for that ref.
    """
    from .catalog import get_ref_snapshot

    if in_scope:
        branch_snap = get_ref_snapshot(table, branch)
        if branch_snap is not None:
            return branch_snap
    if pin_tag:
        tag_snap = get_ref_snapshot(table, pin_tag)
        if tag_snap is not None:
            return tag_snap
    return get_ref_snapshot(table, base_ref)


def read_input(table, branch: str, base_ref: str, pin_tag: str | None, in_scope: bool):
    """Arrow materialization of the resolved input snapshot."""
    snapshot_id = resolve_input_snapshot(table, branch, base_ref, pin_tag, in_scope)
    return _scan(table, snapshot_id) if snapshot_id is not None else table.scan().to_arrow()


def _scan(table, snapshot_id: int):
    return table.scan(snapshot_id=snapshot_id).to_arrow()


def data_stale_models(cfg, catalog, graph) -> list[str]:
    """Models whose upstream data moved since the model last built (main).

    A model is stale when it has no main snapshot yet, or when any upstream
    (model dependency or input table) has a main snapshot strictly newer
    than the model's. Snapshot wall-clock timestamps are the signal — no
    extra recorded state. Downstream closure is the caller's job (the same
    compute_scope that closes over edited models).
    """
    def main_ts(table_id: str) -> int | None:
        try:
            table = catalog.load_table(table_id)
        except Exception:  # noqa: BLE001 — not materialized yet
            return None
        from .catalog import get_ref_snapshot

        head = get_ref_snapshot(table, "main")
        if head is None:
            return None
        snapshot = table.snapshot_by_id(head)
        return snapshot.timestamp_ms if snapshot else None

    stale: list[str] = []
    for name, model in graph.models.items():
        model_ts = main_ts(table_for_model(cfg, name))
        if model_ts is None:
            stale.append(name)
            continue
        for upstream in list(model.depends_on) + list(model.upstream_tables):
            up_id = relation_id(cfg, upstream)
            up_ts = main_ts(up_id)
            if up_ts is not None and up_ts > model_ts:
                stale.append(name)
                break
    return sorted(stale)
