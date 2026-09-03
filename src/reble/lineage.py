"""Model registry + SQLGlot lineage. No dbt dependency — ever.

Model contract (DECISIONS.md §1): plain SQL files under models/**/*.sql,
one file = one model, file stem = model name. Semantics live in a minimal
header comment block:

    -- model: mart_orders      (optional; defaults to file name)
    -- kind: table | view
    -- key: order_id           (diff key)

Lineage = SQLGlot over the registry. A parsed table reference matching a
registry model name is an edge; anything else is an upstream input (pinned
via Iceberg tags at run time). Unparseable SQL → exit 6.

`kind: incremental` is deliberately absent: every run fully rebuilds its
scope (replace, never append), so an "incremental" kind would be a lie.
It returns when watermark / insert-overwrite execution is real.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

from .errors import LineageError

_KINDS = ("table", "view")
_HEADER_RE = re.compile(r"^\s*--\s*([A-Za-z_][\w-]*)\s*:\s*(.+?)\s*$")


def parse_header(sql: str) -> dict[str, str]:
    """Parse the leading `-- key: value` comment block."""
    header: dict[str, str] = {}
    for line in sql.splitlines():
        if not line.strip():
            continue
        if not line.lstrip().startswith("--"):
            break
        match = _HEADER_RE.match(line)
        if match:
            header[match.group(1).lower()] = match.group(2).strip()
    return header


def ast_hash(sql: str, dialect: str = "duckdb") -> str:
    """SHA-256 over a canonicalized SQLGlot AST rendering.

    Canonicalization: comments stripped, unquoted identifiers lowercased,
    pretty-printing off. Cosmetic edits (whitespace, comments, casing) hash
    identically — they never trigger a run.
    """
    tree = parse_model_sql(sql, dialect)
    canonical = _canonicalize(tree)
    rendered = canonical.sql(dialect=dialect, pretty=False, comments=False)
    return hashlib.sha256(rendered.encode()).hexdigest()


def parse_model_sql(sql: str, dialect: str) -> exp.Expression:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise LineageError(f"Unparseable SQL (exit 6): {exc}") from exc
    if tree is None:
        raise LineageError("Empty SQL statement (exit 6)")
    return tree


def _canonicalize(tree: exp.Expression) -> exp.Expression:
    def normalize(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Identifier) and not node.quoted:
            node.set("this", node.this.lower())
        return node

    return tree.transform(normalize, copy=False)


def table_refs(tree: exp.Expression) -> list[str]:
    """All external table references in the AST, as written (last segment).

    CTE names are excluded — a `with latest as (...)` followed by
    `from latest` is a reference to the CTE, not to an upstream table.
    """
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    return [
        t.name
        for t in tree.find_all(exp.Table)
        if t.name and t.name not in ctes
    ]


@dataclass
class ModelNode:
    name: str
    sql: str
    path: str
    kind: str = "table"  # table | view
    diff_keys: list[str] = field(default_factory=list)  # from `key:` header
    depends_on: list[str] = field(default_factory=list)  # model names
    upstream_tables: list[str] = field(default_factory=list)  # non-model refs


class Graph:
    """Model dependency graph keyed by model name."""

    def __init__(self, models: dict[str, ModelNode]):
        self.models = models

    def parents_of(self, name: str) -> set[str]:
        node = self.models.get(name)
        if node is None:
            return set()
        return set(node.depends_on)

    def children_of(self, name: str) -> set[str]:
        out: set[str] = set()
        for other in self.models.values():
            if name in self.parents_of(other.name):
                out.add(other.name)
        return out

    def downstream_closure(self, names: set[str], depth: int | None = None) -> set[str]:
        """All models reachable from `names` via reverse edges (exclusive of `names`).

        depth=None is unbounded; a depth cap stops expansion and callers mark
        deeper tables stale (spec: --depth N).
        """
        frontier = list(names)
        seen: set[str] = set()
        level = 0
        while frontier and (depth is None or level < depth):
            next_frontier: list[str] = []
            for node_name in frontier:
                for child in self.children_of(node_name):
                    if child not in seen and child not in names:
                        seen.add(child)
                        next_frontier.append(child)
            frontier = next_frontier
            level += 1
        return seen

    def upstream_of(self, names: set[str]) -> set[str]:
        out: set[str] = set()
        for name in names:
            out |= self.parents_of(name)
        return out - names


def build_graph(models_path: Path, dialect: str = "duckdb") -> Graph:
    """Scan models/**/*.sql, parse headers, resolve lineage via SQLGlot.

    Two passes: pass 1 parses every file (registry of names); pass 2 classifies
    each table reference as an edge (registry match) or an upstream input.
    A reference matching a model name by its last segment resolves to that
    model; unqualified refs matching nothing are upstream inputs.
    """
    if not models_path.exists():
        raise LineageError(
            f"models directory not found: {models_path} "
            "(set lineage.models_path in reble.yml)"
        )

    files = sorted(models_path.rglob("*.sql"))
    if not files:
        raise LineageError(f"no .sql models found under {models_path}")

    # Pass 1: registry.
    parsed: list[tuple[ModelNode, exp.Expression]] = []
    models: dict[str, ModelNode] = {}
    for sql_file in files:
        sql = sql_file.read_text()
        header = parse_header(sql)
        name = header.get("model") or sql_file.stem
        kind = (header.get("kind") or "table").lower()
        if kind not in _KINDS:
            raise LineageError(
                f"{sql_file}: unknown kind '{kind}' (expected table|view)"
            )
        keys = [k.strip() for k in re.split(r"[,;]", header.get("key", "")) if k.strip()]
        tree = parse_model_sql(sql, dialect)  # unparseable → LineageError (exit 6)
        node = ModelNode(
            name=name,
            sql=sql,
            path=str(sql_file),
            kind=kind,
            diff_keys=keys,
        )
        if name in models:
            raise LineageError(
                f"duplicate model name '{name}': {models[name].path} and {sql_file}"
            )
        models[name] = node
        parsed.append((node, tree))

    # Pass 2: edges.
    for node, tree in parsed:
        for ref in table_refs(tree):
            if ref in models:
                if ref != node.name:
                    node.depends_on.append(ref)
            else:
                if ref not in node.upstream_tables:
                    node.upstream_tables.append(ref)
    node.depends_on = sorted(set(node.depends_on))
    node.upstream_tables = sorted(set(node.upstream_tables))

    return Graph(models=models)
