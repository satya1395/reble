"""Local execution engines (spec: engines; DECISIONS.md).

v0 primary: pyiceberg + DuckDB, ours end to end. Every model executes as
CTAS-then-overwrite on the branch ref (full rebuild, replace-never-append).
Spark runs the same contract through Iceberg's SQL extensions.

Input resolution is AST-based: every table reference in the model SQL is
rewritten to a DuckDB view over the appropriate snapshot — branch ref for
in-scope models, pin tag for upstream inputs, base otherwise.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import pyarrow as pa
from sqlglot import exp

from .duckio import DuckIo
from .errors import RebleError
from .lineage import ModelNode, ast_hash, parse_model_sql
from .relations import relation_id, resolve_input_snapshot, table_for_model
from .sparkio import SparkIo


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


def rewrite_refs(tree: exp.Expression, view_for: dict[str, str]) -> exp.Expression:
    """Replace each resolved Table node with a quoted view reference."""

    def substitute(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table) and node.name in view_for:
            return exp.Table(this=exp.to_identifier(view_for[node.name], quoted=True))
        return node

    return tree.transform(substitute)


def input_view_name(ref: str, index: int) -> str:
    return f"reble_in_{index}_{re.sub(r'[^A-Za-z0-9_]', '_', ref)}"


def load_input(catalog, table_id: str, ref: str, model: ModelNode):
    try:
        return catalog.load_table(table_id)
    except Exception as exc:
        raise RebleError(
            f"model {model.name}: input '{ref}' ({table_id}) not found in catalog — "
            "upstream inputs must exist before `reble run`"
        ) from exc


class SparkEngine:
    """Embedded Spark runner. Same interface and guarantees as DuckDbEngine.

    Reads: Spark version travel over the shared catalog (pinned snapshot
    ids). Writes: Iceberg SQL `INSERT OVERWRITE tbl.branch` with provenance
    in the snapshot summary via `spark.sql.iceberg.snapshot-property.*`.
    The zero-row seed + branch creation go through pyiceberg — identical to
    the DuckDB path — so refs behave the same regardless of engine.
    """

    name = "spark (local)"

    def __init__(self, cfg, catalog):
        self.cfg = cfg
        self.catalog = catalog
        self.io = SparkIo(cfg.engines.spark, cfg.warehouse.catalog)
        self.warnings: list[str] = []

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
        self.warnings = []

        spark = self.io.connect()
        query = self._build_query(spark, model, tree, graph, branch, base_ref, pin_tag, pin_inputs)

        provenance = {
            "reble.model": model.name,
            "reble.ast_hash": ast_hash(model.sql, dialect),
            "reble.branch": branch,
        }
        if run_id:
            provenance["reble.run_id"] = run_id
        if changeset_id:
            provenance["reble.changeset"] = changeset_id

        rows = self._write(spark, model, branch, query, provenance)
        return RunResult(
            model=model.name,
            status="ran",
            kind=model.kind,
            rows_written=rows,
            duration_ms=int((time.time() - started) * 1000),
            ast_hash=provenance["reble.ast_hash"],
            sql=model.sql,
        )

    def _build_query(self, spark, model, tree, graph, branch, base_ref, pin_tag, pin_inputs) -> str:
        view_for: dict[str, str] = {}
        for node in tree.find_all(exp.Table):
            ref = node.name
            if ref in view_for or ref == model.name:
                continue
            rel_model = graph.models.get(ref)
            table_id = relation_id(self.cfg, ref)
            table = load_input(self.catalog, table_id, ref, model)
            tag = pin_tag(ref) if pin_inputs else None
            in_scope = rel_model is not None and ref != model.name
            snapshot_id = resolve_input_snapshot(table, branch, base_ref, tag, in_scope)
            view = input_view_name(ref, len(view_for))
            self.io.register_snapshot(view, table_id, snapshot_id)
            view_for[ref] = view
        rewritten = rewrite_refs(tree, view_for)
        return rewritten.sql(dialect="spark")

    def _write(self, spark, model, branch, query: str, provenance: dict[str, str]) -> int:
        from .catalog import ensure_branch, get_ref_snapshot

        table_id = table_for_model(self.cfg, model.name)
        qualified = self.io.qualified(table_id)
        try:
            self.catalog.load_table(table_id)
            exists = True
        except Exception:  # noqa: BLE001 — table not found = new model
            exists = False

        seed_props = {**provenance, "reble.seed": "true"}
        if not exists:
            # Same contract as the DuckDB path: tables are born with one
            # zero-row seed snapshot on main (marked reble.seed), so the
            # branch ref has a snapshot to hang from and main stays empty
            # until promote.
            self._write_to(spark, query, target=qualified, provenance=seed_props,
                           create=True, limit_zero=True)
        elif not self.catalog.load_table(table_id).metadata.snapshots:
            self._write_to(spark, query, target=qualified, provenance=seed_props,
                           limit_zero=True)

        ensure_branch(self.catalog, table_id, branch, "main")

        # Branch identifier: `.branch_<name>` — the write lands on the branch
        # ref, not main. Backticks: changeset ids may contain dashes.
        try:
            self._write_to(spark, query, target=f"{qualified}.`branch_{branch}`",
                           provenance=provenance, overwrite=True)
        except Exception as exc:
            raise RebleError(f"model {model.name}: spark write failed: {exc}") from exc

        table = self.catalog.load_table(table_id)
        head = get_ref_snapshot(table, branch)
        if head is not None:
            snapshot = table.snapshot_by_id(head)
            summary = getattr(snapshot, "summary", None) if snapshot else None
            props = dict(getattr(summary, "additional_properties", {}) or {})
            try:
                return int(props.get("total-records", 0))
            except (TypeError, ValueError):
                return 0
        return 0

    @staticmethod
    def _write_to(spark, query, target, provenance, create=False,
                  overwrite=False, limit_zero=False):
        """DataFrame write — the only path that carries snapshot properties
        on Spark 3.5 (SQL `SUMMARY` clauses arrive in Spark 4.1; session
        `spark.sql.iceberg.snapshot-property.*` confs are write options in
        Iceberg 1.6, not session keys)."""
        writer = spark.sql(query)
        if limit_zero:
            writer = writer.limit(0)
        writer = writer.writeTo(target)
        if create:
            writer = writer.using("iceberg")
        for key, value in provenance.items():
            writer = writer.option(f"snapshot-property.{key}", str(value))
        if create:
            writer.create()
        elif overwrite:
            # Unpartitioned Reble tables: dynamic overwrite = full replace.
            writer.overwritePartitions()
        else:
            writer.append()


class DuckDbEngine:
    name = "duckdb (local)"

    def __init__(self, cfg, catalog, reble_dir=None):
        self.cfg = cfg
        self.catalog = catalog
        self.io = DuckIo(cfg.engines.duckdb, reble_dir)
        self.warnings: list[str] = []  # per-run io warnings (fallbacks)

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
        self.warnings = []

        con = self.io.connect()
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
            snapshot_id = resolve_input_snapshot(table, branch, base_ref, tag, in_scope)
            view = f"reble_in_{len(view_for)}_{re.sub(r'[^A-Za-z0-9_]', '_', ref)}"
            self.io.register_snapshot(con, view, table, snapshot_id, self.warnings)
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
