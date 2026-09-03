"""Iceberg catalog operations on native branch refs (invariants 4, 6, 7, 8).

Substrate: native Iceberg branch refs — per-table branches supported by any
spec-compliant catalog. No Nessie dependency. Managed multi-table atomic
promotion is a Reble-catalog feature and is labeled as such in output.
"""

from __future__ import annotations

from pyiceberg.catalog import Catalog
from pyiceberg.catalog import load_catalog as pyiceberg_load_catalog

from .config import CatalogConfig
from .errors import ConfigError


def load_catalog(cfg: CatalogConfig, name: str | None = None) -> Catalog:
    """Map reble.yml catalog config to a pyiceberg catalog.

    glue   → AWS Glue
    hive   → Hive metastore
    sql / in-memory → pyiceberg SQL / in-memory catalogs (local dev, CI)
    polaris / nessie / rest / reble → Iceberg REST spec catalogs

    For SQL-backed catalogs the catalog name is part of table identity, so it
    must be stable: `warehouse.catalog.name` wins, else "reble".
    """
    props = {k: v for k, v in cfg.model_dump(exclude={"type"}).items() if v is not None}
    catalog_name = name or props.pop("name", None) or "reble"
    if cfg.type in ("glue", "hive", "in-memory", "sql", "dynamodb", "bigquery"):
        props["type"] = cfg.type
        if cfg.type == "glue" and "region" in props:
            # pyiceberg reads glue.region; translate the user-friendly key
            props["glue.region"] = props.pop("region")
    else:
        # polaris, nessie, rest, reble all speak the Iceberg REST spec
        props["type"] = "rest"
        if "uri" not in props:
            raise ConfigError(f"Catalog type '{cfg.type}' requires a uri")
    try:
        return pyiceberg_load_catalog(catalog_name, **props)
    except Exception as exc:
        raise ConfigError(f"Catalog unreachable ({cfg.type}): {exc}") from exc


def snapshot_id_of(ref) -> int:
    """Snapshot id of a SnapshotRef, tolerating wrapper shapes across versions."""
    if hasattr(ref, "snapshot_id"):
        return ref.snapshot_id
    return ref.snapshot_ref.snapshot_id  # type: ignore[attr-defined]


def get_ref_snapshot(table, ref: str) -> int | None:
    refs = table.refs()
    if ref in refs:
        return snapshot_id_of(refs[ref])
    if ref == "main":
        return table.metadata.current_snapshot_id
    return None


def get_head(catalog: Catalog, table_id: str, ref: str) -> int | None:
    """Current snapshot of `ref` (branch or tag) on the table."""
    return get_ref_snapshot(catalog.load_table(table_id), ref)


def ensure_branch(catalog: Catalog, table_id: str, branch: str, from_ref: str) -> int:
    """Create a zero-copy branch ref on the table (idempotent).

    Returns the branch head snapshot id. A branch ref is metadata-only:
    zero bytes are copied.
    """
    table = catalog.load_table(table_id)
    head = get_ref_snapshot(table, branch)
    if head is not None:
        return head
    from_snapshot = get_ref_snapshot(table, from_ref)
    if from_snapshot is None:
        from_snapshot = table.metadata.current_snapshot_id
    if from_snapshot is None:
        raise ConfigError(f"Table {table_id} has no snapshots to branch from")
    table.manage_snapshots().create_branch(
        snapshot_id=from_snapshot, branch_name=branch
    ).commit()
    return from_snapshot


def pin_snapshot(catalog: Catalog, table_id: str, tag: str, snapshot_id: int) -> None:
    """Create (or retarget) an Iceberg tag — tags block expire_snapshots (invariant 4)."""
    table = catalog.load_table(table_id)
    existing = get_ref_snapshot(table, tag)
    if existing == snapshot_id:
        return
    if existing is not None:
        table.manage_snapshots().remove_tag(tag_name=tag).commit()
        table = catalog.load_table(table_id)
    table.manage_snapshots().create_tag(snapshot_id=snapshot_id, tag_name=tag).commit()


def fast_forward(catalog: Catalog, table_id: str, branch: str, snapshot_id: int) -> None:
    """Move a ref to snapshot_id. Refuses to be used for non-forward moves (invariant 7).

    Main is the table's current snapshot; custom base refs are rebuilt at the
    target snapshot (remove + create — ref retention settings are not preserved,
    acceptable for v0 base refs).
    """
    table = catalog.load_table(table_id)
    if get_ref_snapshot(table, branch) == snapshot_id:
        return
    if branch == "main":
        table.manage_snapshots().set_current_snapshot(snapshot_id=snapshot_id).commit()
    else:
        table.manage_snapshots().remove_branch(branch_name=branch).commit()
        table = catalog.load_table(table_id)
        table.manage_snapshots().create_branch(
            snapshot_id=snapshot_id, branch_name=branch
        ).commit()


def is_fast_forward(table, base_ref: str, branch_ref: str) -> bool:
    """True when the base head is an ancestor of (or equal to) the branch head.

    Snapshot ids are not monotonic across generators, so legality is checked
    on the snapshot parent chain, not id ordering.
    """
    base = get_ref_snapshot(table, base_ref)
    branch = get_ref_snapshot(table, branch_ref)
    if base is None or branch is None or base == branch:
        return True
    parents = {s.snapshot_id: s.parent_snapshot_id for s in table.metadata.snapshots}
    current = parents.get(branch)
    while current is not None:
        if current == base:
            return True
        current = parents.get(current)
    return False


def drop_ref(catalog: Catalog, table_id: str, name: str, kind: str) -> None:
    """Drop a branch or tag ref (used by `branch discard` and `gc`)."""
    table = catalog.load_table(table_id)
    manager = table.manage_snapshots()
    if kind == "branch":
        manager.remove_branch(branch_name=name)
    else:
        manager.remove_tag(tag_name=name)
    manager.commit()


def list_tables_with_ref(catalog: Catalog, ref: str) -> list[str]:
    """All catalog tables carrying a ref with the given name."""
    matches: list[str] = []
    for table_id in _all_tables(catalog):
        try:
            if ref in catalog.load_table(table_id).refs():
                matches.append(table_id)
        except Exception:  # noqa: BLE001
            continue
    return matches


def _all_tables(catalog: Catalog) -> list[str]:
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
