"""Model registry + SQLGlot lineage (DECISIONS.md §1)."""

from __future__ import annotations

import pytest

from reble.errors import LineageError
from reble.lineage import ast_hash, build_graph, parse_header


def test_header_parsing(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.sql").write_text(
        "-- model: custom_name\n-- kind: view\n-- key: id, org\n"
        "select 1 as id, 2 as org\n"
    )
    graph = build_graph(tmp_path / "models")
    node = graph.models["custom_name"]
    assert node.kind == "view"
    assert node.diff_keys == ["id", "org"]
    # file stem not used when -- model: is given
    assert "a" not in graph.models


def test_incremental_kind_rejected_until_real(tmp_path):
    """The contract refuses to say 'incremental' until it means it."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.sql").write_text(
        "-- kind: incremental\nselect 1\n"
    )
    with pytest.raises(LineageError, match="incremental"):
        build_graph(tmp_path / "models")


def test_defaults_file_stem_and_table_kind(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "thing.sql").write_text("select 1 as x\n")
    node = build_graph(tmp_path / "models").models["thing"]
    assert node.kind == "table"
    assert node.diff_keys == []


def test_edges_vs_upstream_inputs(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "stg.sql").write_text("select * from raw_events\n")
    (tmp_path / "models" / "mart.sql").write_text("select * from stg\n")
    graph = build_graph(tmp_path / "models")
    assert graph.models["stg"].upstream_tables == ["raw_events"]
    assert graph.models["stg"].depends_on == []
    assert graph.models["mart"].depends_on == ["stg"]
    assert graph.models["mart"].upstream_tables == []
    assert graph.children_of("stg") == {"mart"}


def test_unknown_kind_rejected(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.sql").write_text("-- kind: sorcery\nselect 1\n")
    with pytest.raises(LineageError):
        build_graph(tmp_path / "models")


def test_duplicate_model_names_rejected(tmp_path):
    (tmp_path / "models" / "sub").mkdir(parents=True)
    (tmp_path / "models" / "a.sql").write_text("select 1\n")
    (tmp_path / "models" / "sub" / "a.sql").write_text("select 2\n")
    with pytest.raises(LineageError, match="duplicate model name"):
        build_graph(tmp_path / "models")


def test_unparseable_sql_is_exit_6(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "bad.sql").write_text("SELEC FRM nope")
    with pytest.raises(LineageError) as exc:
        build_graph(tmp_path / "models")
    assert exc.value.exit_code == 6


def test_missing_models_dir_is_exit_6(tmp_path):
    with pytest.raises(LineageError) as exc:
        build_graph(tmp_path / "does_not_exist")
    assert exc.value.exit_code == 6


def test_ast_hash_cosmetic_invariance():
    base = "SELECT a, b FROM t WHERE x = 1"
    cosmetic = "select   a,\n  b -- a comment\nfrom T where X = 1"
    assert ast_hash(base) == ast_hash(cosmetic)

    real_change = "SELECT a, b FROM t WHERE x = 2"
    assert ast_hash(base) != ast_hash(real_change)


def test_ast_hash_quoted_identifiers_are_case_sensitive():
    # quoted identifiers must NOT be lowercased into equivalence
    assert ast_hash('select "A" from t') != ast_hash('select "a" from t')


def test_parse_header_stops_at_first_non_comment():
    header = parse_header("-- model: x\n\n-- later: ignored? no, still comment\nselect 1")
    assert header["model"] == "x"


def test_cte_names_are_not_upstream_tables():
    """A CTE reference (`with x as (...) select * from x`) is not an input
    table — regression: models with CTEs failed with 'input not found'."""
    from reble.lineage import parse_model_sql, table_refs

    tree = parse_model_sql(
        "with latest as (select *, row_number() over "
        "(partition by id order by ts desc) as rn from raw_events) "
        "select id from latest where rn = 1",
        "duckdb",
    )
    assert table_refs(tree) == ["raw_events"]
