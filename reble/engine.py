"""Local execution engines (spec: engines; DECISIONS.md).

v0 primary: pyiceberg + DuckDB, ours end to end. Every model executes as
CTAS-then-overwrite on the branch ref: `table` and `view` kinds materialize
the full result; `incremental` always full-refreshes inside branches (spec
rule). Spark stays a config-declared runner behind this interface, stubbed.

Input resolution is AST-based: every table reference in the model SQL is
rewritten to a DuckDB view over the appropriate snapshot — branch ref for
in-scope models, pin tag for upstream inputs, base otherwise.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import duckdb
import pyarrow as pa
from sqlglot import exp

from .errors import RebleError
from .lineage import ModelNode, ast_hash, parse_model_sql
from .relations import read_input, relation_id, table_for_model


@dataclass
class RunResult:
    model: str
    status: str  # ran | skipped | error
    kind: str = "table"
    rows_written: int = 0
    duration_ms: int = 0
    error: str | None = None
    ast_hash: str | None = None
    sql: str | None = None  # SQL bundle — provenance for ran models


class SparkEngine:
    """Config-declared runner, stubbed for v0. Same interface as DuckDbEngine."""

    name = "spark (stubbed)"

    def __init__(self, cfg, catalog):
        self.cfg = cfg
        self.catalog = catalog

    def execute_model(
        self, model, graph, branch, base_ref, pin_tag, pin_inputs,
        run_id=None, changeset_id=None,
    ) -> RunResult:
        return RunResult(
            model=model.name,
            status="error",
            kind=model.kind,
            error="spark runner is declared-but-stubbed in v0 — use compute_policy.prefer: duckdb",
        )


class DuckDbEngine:
    name = "duckdb (local)"

    def __init__(self, cfg, catalog):
        self.cfg = cfg
        self.catalog = catalog

    def execute_model(
        self,
        model: ModelNode,
        graph,
        branch: str,
        base_ref: str,
        pin_tag,  # Callable[[str], str] mapping relation name -> tag name
        pin_inputs: bool,
        run_id: str | None = None,
        changeset_id: str | None = None,
    ) -> RunResult:
        started = time.time()
        dialect = self.cfg.lineage.dialect
        tree = parse_model_sql(model.sql, dialect)  # unparseable → exit 6

        con = duckdb.connect(":memory:")
        try:
            view_for = self._register_inputs(con, model, tree, graph, branch, base_ref, pin_tag, pin_inputs)
            rewritten = self._rewrite_refs(tree, view_for)
            out: pa.Table = con.execute(rewritten.sql(dialect=dialect)).to_arrow_table()
        finally:
            con.close()

        provenance = {
            "reble.model": model.name,
            "reble.ast_hash": ast_hash(model.sql, dialect),
            "reble.branch": branch,
        }
        if run_id:
            provenance["reble.run_id"] = run_id
        if changeset_id:
            provenance["reble.changeset"] = changeset_id

        self._write(model, branch, out, provenance)
        return RunResult(
            model=model.name,
            status="ran",
            kind=model.kind,
            rows_written=out.num_rows,
            duration_ms=int((time.time() - started) * 1000),
            ast_hash=provenance["reble.ast_hash"],
            sql=model.sql,
        )

    def _register_inputs(
        self, con, model, tree, graph, branch, base_ref, pin_tag, pin_inputs
    ) -> dict[str, str]:
        """Resolve every distinct table ref to a registered DuckDB view."""
        view_for: dict[str, str] = {}
        for node in tree.find_all(exp.Table):
            ref = node.name
            if ref in view_for or ref == model.name:
                continue
            rel_model = graph.models.get(ref)
            table_id = relation_id(self.cfg, ref)
            table = self._load_input(table_id, ref, model)
            tag = pin_tag(ref) if pin_inputs else None
            in_scope = rel_model is not None and ref != model.name
            data = read_input(table, branch, base_ref, tag, in_scope)
            view = f"reble_in_{len(view_for)}_{re.sub(r'[^A-Za-z0-9_]', '_', ref)}"
            con.register(view, data)
            view_for[ref] = view
        return view_for

    def _load_input(self, table_id: str, ref: str, model: ModelNode):
        try:
            return self.catalog.load_table(table_id)
        except Exception as exc:
            raise RebleError(
                f"model {model.name}: input '{ref}' ({table_id}) not found in catalog — "
                "upstream inputs must exist before `reble run`"
            ) from exc

    @staticmethod
    def _rewrite_refs(tree: exp.Expression, view_for: dict[str, str]) -> exp.Expression:
        """Replace each resolved Table node with a quoted view reference."""

        def substitute(node: exp.Expression) -> exp.Expression:
            if isinstance(node, exp.Table) and node.name in view_for:
                return exp.Table(this=exp.to_identifier(view_for[node.name], quoted=True))
            return node

        return tree.transform(substitute)

    def _write(
        self,
        model: ModelNode,
        branch: str,
        out: pa.Table,
        provenance: dict[str, str] | None = None,
    ) -> None:
        """CTAS semantics: overwrite the branch head; create the table if new.

        Never writes real data to the base ref. A snapshot-less table cannot
        take a branch write in Iceberg, so new models are seeded with one
        zero-row snapshot on main (marked `reble.seed`), branched, then
        written on the branch — main stays empty until promote. Provenance
        keys ride the snapshot summary, so they travel with the snapshot to
        main at promote time.
        """
        import warnings

        from pyiceberg.io.pyarrow import NameMapping, pyarrow_to_schema

        table_id = table_for_model(self.cfg, model.name)
        try:
            table = self.catalog.load_table(table_id)
        except Exception:  # noqa: BLE001 — table not found = new model
            # Assign field ids (1..n) in the arrow metadata so schema conversion
            # is deterministic; the name-mapping property then lets pyiceberg
            # write plain (field-id-less) arrow data by name. Top-level fields
            # only — v0 model outputs are flat.
            annotated = pa.schema(
                [f.with_metadata({"PARQUET:field_id": str(i)}) for i, f in enumerate(out.schema, 1)],
                metadata=out.schema.metadata,
            )
            schema = pyarrow_to_schema(annotated)
            mapping = NameMapping(
                [{"field-id": f.field_id, "names": [f.name]} for f in schema.fields]
            )
            table = self.catalog.create_table(
                table_id,
                schema=schema,
                properties={"schema.name-mapping.default": mapping.model_dump_json()},
            )

        if not table.metadata.snapshots:
            seed_props = {**(provenance or {}), "reble.seed": "true"}
            table.append(out.slice(0, 0), snapshot_properties=seed_props)  # zero-row seed on main
            table = self.catalog.load_table(table_id)
            table.manage_snapshots().create_branch(
                snapshot_id=table.metadata.current_snapshot_id, branch_name=branch
            ).commit()
            table = self.catalog.load_table(table_id)

        # Incremental models full-refresh inside branches (spec rule); the
        # overwrite below is that full refresh — announced by the caller.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Delete operation did not match any records")
            table.overwrite(out, snapshot_properties=provenance, branch=branch)
