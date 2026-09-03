"""Scope inference: edited ∪ downstream closure, pins, depth cap."""

from __future__ import annotations

from reble.lineage import ModelNode
from reble.scope import compute_scope, detect_changed_models


def _graph(edges: dict[str, list[str]], upstream: dict[str, list[str]] | None = None):
    from reble.lineage import Graph

    upstream = upstream or {}
    models = {
        name: ModelNode(
            name=name,
            sql=f"select 1 as {name}",
            path=f"models/{name}.sql",
            depends_on=list(deps),
            upstream_tables=upstream.get(name, []),
        )
        for name, deps in edges.items()
    }
    return Graph(models=models)


def test_scope_is_edited_union_downstream_closure():
    graph = _graph({
        "stg": [],          # edited
        "mid": ["stg"],     # downstream
        "mart": ["mid"],    # downstream of downstream
        "other": [],        # untouched
    })
    scope = compute_scope(graph, ["stg"])
    assert scope.edited == ["stg"]
    assert scope.downstream == ["mart", "mid"]
    assert scope.scope == ["stg", "mart", "mid"]
    assert "other" not in scope.scope


def test_depth_cap_marks_deeper_tables_stale():
    graph = _graph({"a": [], "b": ["a"], "c": ["b"]})
    scope = compute_scope(graph, ["a"], depth=1)
    assert scope.downstream == ["b"]
    assert scope.stale_by_depth == ["c"]


def test_pinned_inputs_include_out_of_scope_models_and_raw_refs():
    graph = _graph(
        edges={"stg": [], "mart": ["stg"], "dim": []},
        upstream={"stg": ["raw_events"], "mart": ["dim"]},
    )
    scope = compute_scope(graph, ["stg"])
    # raw_events (non-model) and dim (model outside scope) are both pinned
    assert set(scope.pinned_inputs) == {"raw_events", "dim"}


def test_empty_edited_is_legal_empty_scope():
    scope = compute_scope(_graph({"a": []}), [])
    assert scope.is_empty


def test_detect_changed_models_by_ast_hash():
    graph = _graph({"a": [], "b": []})
    graph.models["a"].sql = "select 1 as a"
    graph.models["b"].sql = "select 2 as b"
    changed, hashes = detect_changed_models(graph, {"a": ast_hash_of("select 1 as a")})
    assert changed == ["b"]
    assert set(hashes) == {"a", "b"}


def ast_hash_of(sql: str) -> str:
    from reble.lineage import ast_hash

    return ast_hash(sql)
