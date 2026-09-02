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


def read_input(table, branch: str, base_ref: str, pin_tag: str | None, in_scope: bool):
    """Branch read for in-scope models, tag read for pinned inputs (invariant 4).

    Resolution order: branch ref → pin tag → base ref → current snapshot.
    The tag is the source of truth for pinned reads; the snapshot id is an
    in-process detail of asking the catalog for that ref.
    """
    from .catalog import get_ref_snapshot

    branch_snap = get_ref_snapshot(table, branch)
    if in_scope and branch_snap is not None:
        return _scan(table, branch_snap)
    if pin_tag:
        tag_snap = get_ref_snapshot(table, pin_tag)
        if tag_snap is not None:
            return _scan(table, tag_snap)
    base_snap = get_ref_snapshot(table, base_ref)
    if base_snap is not None:
        return _scan(table, base_snap)
    return table.scan().to_arrow()


def _scan(table, snapshot_id: int):
    return table.scan(snapshot_id=snapshot_id).to_arrow()
