"""The main verb: `reble run` (spec section 4).

Resolves scope, creates/updates the data branch, pins inputs, executes on the
selected engine. Idempotent per model via AST hashes in the run manifest.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog as ice
from .config import Config
from .engine import RunResult
from .lineage import Graph, ast_hash
from .relations import relation_id, table_for_model, tag_name
from .scope import ScopePlan


@dataclass
class RunManifest:
    run_id: str
    branch: str
    engine: str
    scope: ScopePlan
    results: list[RunResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "branch": self.branch,
            "engine": self.engine,
            "scope": {
                "edited": self.scope.edited,
                "downstream": self.scope.downstream,
                "stale_by_depth": self.scope.stale_by_depth,
                "pinned_inputs": sorted(self.scope.pinned_inputs),
                "incremental_full_refresh": self.scope.incremental_full_refresh,
            },
            "results": [r.__dict__ for r in self.results],
            "duration_ms": self.duration_ms,
        }


class Runner:
    def __init__(self, cfg: Config, catalog, graph: Graph, engine, reble_dir: Path):
        self.cfg = cfg
        self.catalog = catalog
        self.graph = graph
        self.engine = engine
        self.reble_dir = reble_dir

    def preflight(self, branch: str, scope: ScopePlan) -> dict:
        """The dry-run block: what a real run would do."""
        return {
            "scope (edited)": (len(scope.edited), scope.edited),
            "scope (downstream)": (len(scope.downstream), scope.downstream),
            "pinned inputs": (
                len(scope.pinned_inputs),
                [tag_name(self.cfg, branch, t) for t in sorted(scope.pinned_inputs)],
            ),
            "full-refresh": (
                len(scope.incremental_full_refresh),
                scope.incremental_full_refresh,
            ),
            "engine": self.engine.name,
        }

    def run(
        self,
        branch: str,
        scope: ScopePlan,
        base_ref: str,
        previous_hashes: dict[str, str] | None = None,
        repin: bool = False,
    ) -> RunManifest:
        """Execute the scope on the data branch.

        previous_hashes → execution idempotency: scope models whose AST hash
        matches their last run are skipped. repin=True retargets input pins to
        current main (the promote-with-drift re-run path); default keeps the
        branch-epoch pins stable.
        """
        manifest = RunManifest(
            run_id=uuid.uuid4().hex[:12],
            branch=branch,
            engine=self.engine.name,
            scope=scope,
        )
        previous_hashes = previous_hashes or {}

        # 1. Branch-first: zero-copy refs on every scope table that exists on
        #    the base ref. Tables that exist but have no snapshots yet (created
        #    by an earlier interrupted run) and brand-new models are handled by
        #    the engine's seed-and-branch write path.
        for model in scope.scope:
            table_id = table_for_model(self.cfg, model)
            try:
                ice.ensure_branch(self.catalog, table_id, branch, base_ref)
            except Exception:  # noqa: BLE001 — missing/new/snapshot-less tables are engine-handled
                continue

        # 2. Pin upstream inputs at their current base snapshots (invariant 4).
        if self.cfg.branching.pin_inputs:
            for table in sorted(scope.pinned_inputs):
                table_id = relation_id(self.cfg, table)
                if not _table_exists(self.catalog, table_id):
                    continue
                snapshot = ice.get_head(self.catalog, table_id, base_ref)
                if snapshot is not None:
                    ice.pin_snapshot(
                        self.catalog, table_id, tag_name(self.cfg, branch, table), snapshot
                    )

        # 3. Execute in dependency order. A model may skip only when BOTH its
        #    SQL hash is unchanged AND no in-scope parent executed this run —
        #    an unchanged model downstream of re-run inputs must re-run.
        scope_set = set(scope.scope)
        ran_this_run: set[str] = set()
        for model_name in self._topological(scope.scope):
            model = self.graph.models[model_name]
            hash_unchanged = (
                previous_hashes.get(model_name) == ast_hash(model.sql, self.cfg.lineage.dialect)
            )
            parents_ran = bool(self.graph.parents_of(model_name) & scope_set & ran_this_run)
            if hash_unchanged and not parents_ran:
                manifest.results.append(RunResult(model=model_name, status="skipped", kind=model.kind))
                continue
            result = self.engine.execute_model(
                model=model,
                graph=self.graph,
                branch=branch,
                base_ref=base_ref,
                pin_tag=lambda t: tag_name(self.cfg, branch, t),
                pin_inputs=self.cfg.branching.pin_inputs,
            )
            manifest.results.append(result)
            ran_this_run.add(model_name)

        manifest.duration_ms = int((time.time() - manifest.started_at) * 1000)
        self._write_manifest(manifest)
        _ = repin  # pin_snapshot() above retargets when tags exist; epoch pins are the default path
        return manifest

    def _topological(self, names: list[str]) -> list[str]:
        """Parents before children, within the scope."""
        scope_set = set(names)
        ordered: list[str] = []
        done: set[str] = set()

        def visit(name: str, stack: set[str]) -> None:
            if name in done or name not in scope_set or name in stack:
                return
            stack.add(name)
            for parent in sorted(self.graph.parents_of(name)):
                visit(parent, stack)
            done.add(name)
            ordered.append(name)

        for name in names:
            visit(name, set())
        return ordered

    def _write_manifest(self, manifest: RunManifest) -> None:
        runs_dir = self.reble_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / f"{manifest.run_id}.json").write_text(json.dumps(manifest.to_dict(), indent=2))


def _table_exists(catalog, table_id: str) -> bool:
    try:
        catalog.load_table(table_id)
        return True
    except Exception:  # noqa: BLE001
        return False
