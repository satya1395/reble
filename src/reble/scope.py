"""Scope inference: AST-changed models ∪ downstream closure (invariant 3).

Also computes pinned inputs: upstream tables (models outside scope, sources,
seeds) that get Iceberg tag pins at run time (invariant 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lineage import Graph, ast_hash


@dataclass
class ScopePlan:
    edited: list[str] = field(default_factory=list)
    downstream: list[str] = field(default_factory=list)
    stale_by_depth: list[str] = field(default_factory=list)  # cut off by --depth
    pinned_inputs: dict[str, str] = field(default_factory=dict)  # relation -> model/table name

    @property
    def scope(self) -> list[str]:
        return self.edited + self.downstream

    @property
    def is_empty(self) -> bool:
        return not self.scope


def detect_changed_models(
    graph: Graph,
    previous_hashes: dict[str, str],
    dialect: str = "duckdb",
) -> tuple[list[str], dict[str, str]]:
    """AST-hash every model's SQL; a model changed iff hash differs from previous.

    Cosmetic edits (whitespace, comments, casing) hash identically — never
    trigger a run (invariant 3). Returns (changed names, new hash map).
    """
    changed: list[str] = []
    new_hashes: dict[str, str] = {}
    for name, model in graph.models.items():
        if not model.sql.strip():
            continue
        h = ast_hash(model.sql, dialect)
        new_hashes[name] = h
        if previous_hashes.get(name) != h:
            changed.append(name)
    return sorted(changed), new_hashes


def compute_scope(
    graph: Graph,
    changed: list[str],
    depth: int | None = None,
) -> ScopePlan:
    """Scope = AST-changed ∪ downstream closure (invariant 3).

    depth caps the cascade; models cut off by the cap are reported as stale.
    New models (not present in previous run) whose tables don't exist on main
    are handled by the runner; scope treats them like any edited model.
    """
    changed_set = set(changed)
    if not changed_set:
        return ScopePlan()

    closure = graph.downstream_closure(changed_set)
    stale: list[str] = []
    if depth is not None:
        capped = graph.downstream_closure(changed_set, depth=depth)
        stale = sorted(closure - capped)
        closure = capped

    scope = changed_set | closure
    pinned: dict[str, str] = {}
    for name in sorted(scope):
        for parent in graph.upstream_of({name}):
            if parent not in scope and parent in graph.models:
                pinned[parent] = parent
        for table in graph.models[name].upstream_tables:
            if table not in pinned:
                pinned[table] = table

    return ScopePlan(
        edited=sorted(changed_set),
        downstream=sorted(closure),
        stale_by_depth=stale,
        pinned_inputs=pinned,
    )
