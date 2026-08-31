import pytest

from reble.errors import ProjectError
from reble.models import (canonical, deps_of, fingerprint, fingerprints,
                          load_models, topo_order, upstream_closure)

M = {
    "demo.clean": "WITH v AS (SELECT id, amount FROM raw.orders WHERE amount > 0)\nSELECT * FROM v",
    "demo.totals": "SELECT COUNT(*) AS n, SUM(amount) AS total FROM demo.clean",
    "demo.other": "SELECT 1 AS x",
}


def test_deps_exclude_ctes():
    assert deps_of(M["demo.clean"]) == {"raw.orders"}
    assert deps_of(M["demo.totals"]) == {"demo.clean"}


def test_topo_order_and_cycle():
    order = topo_order(M)
    assert order.index("demo.clean") < order.index("demo.totals")
    cyc = {"a.x": "SELECT * FROM a.y", "a.y": "SELECT * FROM a.x"}
    with pytest.raises(ProjectError, match="cycle"):
        topo_order(cyc)


def test_fingerprint_cosmetic_semantic_cascade():
    base = fingerprint("demo.totals", M)
    cosmetic = {**M, "demo.totals":
                "select  COUNT(*) as n,\n SUM(amount) AS total  from demo.clean -- hi"}
    semantic = {**M, "demo.totals": M["demo.totals"].replace("COUNT(*)", "COUNT(DISTINCT n)")}
    upstream = {**M, "demo.clean": M["demo.clean"].replace("> 0", ">= 0")}
    assert fingerprint("demo.totals", cosmetic) == base
    assert fingerprint("demo.totals", semantic) != base
    assert fingerprint("demo.totals", upstream) != base           # cascade
    assert fingerprint("demo.other", upstream) == fingerprint("demo.other", M)


def test_upstream_closure():
    assert upstream_closure(["demo.clean", "demo.totals"], M) == ["raw.orders"]
    assert upstream_closure(["raw.orders"], M) is None            # not a model


def test_load_models_shape(tmp_path):
    from reble.config import RebleConfig
    (tmp_path / "models" / "demo").mkdir(parents=True)
    (tmp_path / "models" / "demo" / "t.sql").write_text("SELECT 1 AS x")
    cfg = RebleConfig(project_dir=tmp_path)
    assert load_models(cfg) == {"demo.t": "SELECT 1 AS x"}
    (tmp_path / "models" / "loose.sql").write_text("SELECT 1")
    with pytest.raises(ProjectError, match="models/<schema>/<name>.sql"):
        load_models(cfg)
