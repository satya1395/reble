"""Model loading and the SQL-derived graph: deps, order, fingerprints.

A model is a plain SQL file — `models/<schema>/<name>.sql` defines table
`<schema>.<name>`, full stop. No headers, no Jinja, no per-model YAML.
Everything else is read from the SQL itself via SQLGlot (validated in
spikes/04-sqlglot-direct):

- dependencies: table references in the AST (a model's own CTEs excluded)
- execution order: topological sort of the dependency graph
- change detection: fingerprint = hash of the *canonical* AST (whitespace,
  comments, keyword case don't count) composed with upstream fingerprints,
  so upstream changes cascade downstream and nothing else moves
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import sqlglot
from sqlglot import exp

from reble.config import RebleConfig
from reble.errors import ProjectError

DIALECT = "duckdb"
MODELS_DIR = "models"


def load_models(cfg: RebleConfig) -> dict[str, str]:
    """table ident -> SQL text, from models/<schema>/<name>.sql."""
    root = cfg.project_dir / MODELS_DIR
    models: dict[str, str] = {}
    if not root.is_dir():
        return models
    for f in sorted(root.rglob("*.sql")):
        rel = f.relative_to(root)
        if len(rel.parts) != 2:
            raise ProjectError(
                f"model file {rel} must be models/<schema>/<name>.sql "
                "(the path is the table name)")
        table = f"{rel.parts[0]}.{f.stem}"
        models[table] = f.read_text()
    return models


def deps_of(sql: str) -> set[str]:
    """Tables a query reads, excluding its own CTEs."""
    tree = sqlglot.parse_one(sql, read=DIALECT)
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    out: set[str] = set()
    for t in tree.find_all(exp.Table):
        if t.name in ctes and not t.db:
            continue
        out.add(f"{t.db}.{t.name}" if t.db else t.name)
    return out


def canonical(sql: str) -> str:
    """Dialect-normalized SQL: whitespace, comments, keyword case removed."""
    return sqlglot.parse_one(sql, read=DIALECT).sql(dialect=DIALECT, comments=False)


def fingerprint(table: str, models: dict[str, str],
                _cache: dict[str, str] | None = None) -> str:
    """Composite hash: this model's canonical SQL + its upstreams' fingerprints.

    Non-model tables (raw/source data) contribute identity only — data changes
    don't retrigger full models; that's the incremental/watermark story (v0.2).
    """
    cache = _cache if _cache is not None else {}
    if table in cache:
        return cache[table]
    if table not in models:
        cache[table] = hashlib.sha256(table.encode()).hexdigest()
        return cache[table]
    parts = [canonical(models[table])]
    for d in sorted(deps_of(models[table])):
        parts.append(fingerprint(d, models, cache))
    cache[table] = hashlib.sha256("||".join(parts).encode()).hexdigest()
    return cache[table]


def fingerprints(models: dict[str, str]) -> dict[str, str]:
    cache: dict[str, str] = {}
    return {t: fingerprint(t, models, cache) for t in models}


def topo_order(models: dict[str, str]) -> list[str]:
    """Models in dependency order; cycles fail loudly."""
    deps = {t: deps_of(sql) for t, sql in models.items()}
    order: list[str] = []
    state: dict[str, int] = {}          # 1 = visiting, 2 = done

    def visit(t: str, chain: tuple[str, ...]):
        if t not in models or state.get(t) == 2:
            return
        if state.get(t) == 1:
            raise ProjectError(f"dependency cycle: {' -> '.join(chain + (t,))}")
        state[t] = 1
        for d in sorted(deps[t]):
            visit(d, chain + (t,))
        state[t] = 2
        order.append(t)

    for t in sorted(models):
        visit(t, ())
    return order


def upstream_closure(scope: list[str], models: dict[str, str]) -> list[str] | None:
    """All tables the scoped models transitively read (excluding the scope).

    None when a scoped table isn't a known model — its inputs are unknowable
    from lineage, so the caller falls back to pinning everything (safe).
    """
    deps = {t: deps_of(sql) for t, sql in models.items()}
    if any(t not in deps for t in scope):
        return None
    seen: set[str] = set()
    stack = list(scope)
    while stack:
        for d in deps.get(stack.pop(), ()):
            if d not in seen and d not in scope:
                seen.add(d)
                stack.append(d)
    return sorted(seen)
