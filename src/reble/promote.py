"""`reble promote` (invariants 7, 8) and `reble gc`.

Preflight checks two drift signals per branch:
  - input pins: pinned snapshot ≠ current main head → inputs moved
  - base heads: a scope table's main head ≠ the head it was branched from
    → someone wrote to main inside the blast radius
Clean → per-table fast-forwards (atomic only on a reble catalog — labeled in
output). Drifted without --ff-only → re-pin, re-run scope, fresh promote-time
diff, then fast-forward. --ff-only refuses (exit 4).

No three-way merges. Ever. promote (fast-forward or re-run) or discard.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog as ice


@dataclass
class DriftReport:
    table: str
    kind: str  # "pin" | "base"
    pinned_base: int | None
    current_main: int | None

    @property
    def drifted(self) -> bool:
        return self.pinned_base != self.current_main


@dataclass
class PromoteRecord:
    branch: str
    started_at: float = field(default_factory=time.time)
    tables: dict[str, dict] = field(default_factory=dict)
    # status: pending | promoted | failed — re-entrant: an interrupted promote
    # resumes, never double-applies.

    def to_dict(self) -> dict:
        return {"branch": self.branch, "started_at": self.started_at, "tables": self.tables}

    @classmethod
    def from_dict(cls, data: dict) -> PromoteRecord:
        return cls(**data)


class Promoter:
    def __init__(self, cfg, catalog, reble_dir: Path | None = None, persist=None,
                 load_record=None, save_record=None, delete_record=None):
        """`persist` is called after each per-table state mutation so a promote
        interrupted mid-loop resumes without misreading stale base heads as
        drift. Record I/O is injectable: the StateStore wires load/save/delete
        callables so promote records work with any state backend."""
        self.cfg = cfg
        self.catalog = catalog
        self.reble_dir = reble_dir
        self._persist = persist
        self._load_record = load_record
        self._save_record = save_record
        self._delete_record = delete_record

    def preflight(self, branch_state) -> list[DriftReport]:
        reports: list[DriftReport] = []
        for pin in branch_state.pins.values():
            current = ice.get_head(self.catalog, pin.table, branch_state.base_ref)
            reports.append(DriftReport(pin.table, "pin", pin.snapshot_id, current))
        for table_id, base in branch_state.base_heads.items():
            current = ice.get_head(self.catalog, table_id, branch_state.base_ref)
            reports.append(DriftReport(table_id, "base", base, current))
        return reports

    def promote(self, branch_state, ff_only: bool = False) -> dict:
        """Per-table fast-forwards; re-entrant state in .reble/promote.json."""
        record = self._load_record_from_store(branch_state.data_branch)

        def advance(table_id: str, head: int) -> None:
            # The table's base is now main itself — update before anything
            # can observe stale state.
            branch_state.base_heads[table_id] = head
            if self._persist:
                self._persist()

        results: dict[str, dict] = {}
        for table_id in sorted(branch_state.base_heads):
            status = record.tables.get(table_id, {}).get("status")
            if status == "promoted":
                results[table_id] = {"status": "promoted (resumed)"}
                continue
            try:
                branch_head = ice.get_head(self.catalog, table_id, branch_state.data_branch)
                main_head = ice.get_head(self.catalog, table_id, branch_state.base_ref)
                if branch_head is None:
                    results[table_id] = {"status": "skipped", "reason": "no branch head"}
                elif branch_head == main_head:
                    results[table_id] = {"status": "up-to-date"}
                    if main_head is not None:
                        advance(table_id, main_head)
                else:
                    table = self.catalog.load_table(table_id)
                    if not ice.is_fast_forward(table, branch_state.base_ref, branch_state.data_branch):
                        # main diverged from the branch point — refuse, never merge
                        results[table_id] = {"status": "failed", "reason": "non-fast-forward"}
                    else:
                        ice.fast_forward(self.catalog, table_id, branch_state.base_ref, branch_head)
                        results[table_id] = {"status": "promoted", "snapshot": branch_head}
                        advance(table_id, branch_head)
            except Exception as exc:  # noqa: BLE001
                results[table_id] = {"status": "failed", "reason": str(exc)}
            record.tables[table_id] = results[table_id]
            self._save_record_to_store(record)

        terminal = ("promoted", "up-to-date", "promoted (resumed)", "skipped")
        if all(r.get("status") in terminal for r in results.values()):
            self._delete_record_from_store(branch_state.data_branch)
        return results

    def _load_record_from_store(self, branch: str) -> PromoteRecord:
        if self._load_record:
            data = self._load_record(branch)
            if data and data.get("branch") == branch:
                return PromoteRecord.from_dict(data)
        return PromoteRecord(branch=branch)

    def _save_record_to_store(self, record: PromoteRecord) -> None:
        if self._save_record:
            self._save_record(record.to_dict())

    def _delete_record_from_store(self, data_branch: str) -> None:
        if self._delete_record:
            self._delete_record(data_branch)


def orphan_pin_tags(catalog, cfg, active_tags: set[str]) -> list[tuple[str, str]]:
    """Pin tags on catalog tables that no active branch claims (gc correctness)."""
    orphans: list[tuple[str, str]] = []
    prefix = cfg.branching.tag_prefix
    for table_id in _list_tables(catalog):
        try:
            table = catalog.load_table(table_id)
            refs = table.refs()
        except Exception:  # noqa: BLE001
            continue
        for ref_name in refs:
            if ref_name.startswith(prefix) and ref_name not in active_tags:
                orphans.append((table_id, ref_name))
    return orphans


def _list_tables(catalog) -> list[str]:
    out: list[str] = []
    try:
        for ns in catalog.list_namespaces():
            out.extend(catalog.list_tables(ns))
    except Exception:  # noqa: BLE001 — catalogs vary in namespace listing support
        try:
            out.extend(catalog.list_tables())
        except Exception:  # noqa: BLE001
            pass
    return out
