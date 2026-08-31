"""Spike 4: Can SQLGlot alone replace SQLMesh for Reble's needs?

The slim-core hypothesis: models are plain SQL files (filename = table name),
and SQLGlot provides everything we currently get from SQLMesh —

  1. dependency extraction from the AST (CTEs must NOT count as deps)
  2. topological execution order
  3. column-level lineage
  4. change-detection fingerprints (cosmetic edits -> same hash; semantic
     edits -> new hash; upstream changes cascade via composite hashing)
  5. execution: ephemeral in-memory DuckDB over Iceberg-backed Arrow views,
     committing straight to branch refs via the REAL BranchEngine —
     no persistent db.db, no mirror step, no SQLMesh import anywhere.

If all checks pass, SQLMesh can be dropped from the core.
"""

import hashlib
import shutil
import sys
from pathlib import Path

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as sg_lineage

from reble.branches import BranchEngine
from reble.config import RebleConfig

HERE = Path(__file__).parent
RESULTS: list[tuple[str, str]] = []
DIALECT = "duckdb"


def report(step: str, ok: bool, detail: str = ""):
    status = "PASS" if ok else "FAIL"
    RESULTS.append((status, step))
    print(f"[{status}] {step}" + (f" -- {detail}" if detail else ""))


# --- the slim core, in miniature -------------------------------------------

def deps_of(sql: str) -> set[str]:
    """Tables a query reads, excluding its own CTEs."""
    tree = sqlglot.parse_one(sql, read=DIALECT)
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    out = set()
    for t in tree.find_all(exp.Table):
        if t.name in ctes and not t.db:
            continue
        out.add(f"{t.db}.{t.name}" if t.db else t.name)
    return out


def canonical(sql: str) -> str:
    """Dialect-normalized SQL: whitespace, comments, keyword case removed."""
    return sqlglot.parse_one(sql, read=DIALECT).sql(dialect=DIALECT, comments=False)


def fingerprint(table: str, models: dict[str, str],
                cache: dict[str, str] | None = None) -> str:
    """Composite hash: my canonical SQL + my upstreams' fingerprints.
    Upstream changes therefore cascade, exactly like SQLMesh snapshots."""
    cache = cache if cache is not None else {}
    if table in cache:
        return cache[table]
    if table not in models:                      # raw table: identity only
        cache[table] = hashlib.sha256(table.encode()).hexdigest()
        return cache[table]
    parts = [canonical(models[table])]
    for d in sorted(deps_of(models[table])):
        parts.append(fingerprint(d, models, cache))
    cache[table] = hashlib.sha256("||".join(parts).encode()).hexdigest()
    return cache[table]


def topo_order(models: dict[str, str]) -> list[str]:
    order, seen = [], set()

    def visit(t):
        if t in seen or t not in models:
            return
        seen.add(t)
        for d in sorted(deps_of(models[t])):
            visit(d)
        order.append(t)

    for t in sorted(models):
        visit(t)
    return order


def main() -> int:
    # --- fixture: a tiny project, plain SQL files --------------------------
    models = {
        "demo.orders_clean": """
            -- keep only real revenue
            WITH valid AS (
                SELECT id, amount FROM raw.orders WHERE amount > 0
            )
            SELECT * FROM valid
        """,
        "demo.totals": """
            SELECT COUNT(*) AS n, SUM(amount) AS total_amount
            FROM demo.orders_clean
        """,
        "demo.unrelated": "SELECT 1 AS x",
    }

    # --- 1. dependency extraction (CTE 'valid' must not appear) ------------
    try:
        d1 = deps_of(models["demo.orders_clean"])
        d2 = deps_of(models["demo.totals"])
        ok = d1 == {"raw.orders"} and d2 == {"demo.orders_clean"}
        report("1. deps from AST, CTEs excluded", ok, f"{d1} | {d2}")
    except Exception as e:
        report("1. deps from AST, CTEs excluded", False, repr(e))
        return 1

    # --- 2. topological order ----------------------------------------------
    order = topo_order(models)
    ok = order.index("demo.orders_clean") < order.index("demo.totals")
    report("2. topological execution order", ok, str(order))

    # --- 3. column-level lineage -------------------------------------------
    try:
        node = sg_lineage(
            "total_amount", models["demo.totals"],
            schema={"demo": {"orders_clean": {"id": "bigint", "amount": "double"}}},
            dialect=DIALECT,
        )
        walked = [n.name for n in node.walk()]
        ok = any("amount" in w and "total" not in w for w in walked)
        report("3. column lineage: total_amount <- orders_clean.amount", ok,
               f"walk={walked}")
    except Exception as e:
        report("3. column lineage: total_amount <- orders_clean.amount", False, repr(e))

    # --- 4. fingerprints: cosmetic vs semantic vs cascade -------------------
    try:
        base = fingerprint("demo.totals", models)
        cosmetic = dict(models)
        cosmetic["demo.totals"] = (
            "select   COUNT(*) as n,\n  sum(amount) AS total_amount"
            "  from demo.orders_clean  -- a comment\n")
        semantic = dict(models)
        semantic["demo.totals"] = models["demo.totals"].replace(
            "COUNT(*)", "COUNT(DISTINCT id)")
        upstream = dict(models)
        upstream["demo.orders_clean"] = models["demo.orders_clean"].replace(
            "> 0", ">= 0")
        ok = (fingerprint("demo.totals", cosmetic) == base
              and fingerprint("demo.totals", semantic) != base
              and fingerprint("demo.totals", upstream) != base
              and fingerprint("demo.unrelated", upstream)
                  == fingerprint("demo.unrelated", models))
        report("4. fingerprints: cosmetic same, semantic+cascade differ, "
               "unrelated stable", ok)
    except Exception as e:
        report("4. fingerprints", False, repr(e))

    # --- 5. execution over Iceberg views, straight onto a branch ref -------
    try:
        proj = HERE / "wh"
        shutil.rmtree(proj, ignore_errors=True)
        (proj / "warehouse").mkdir(parents=True)
        eng = BranchEngine(RebleConfig(project_dir=proj))
        eng.write("raw.orders", pa.table({
            "id": pa.array([1, 2, 3], pa.int64()),
            "amount": pa.array([10.0, -5.0, 20.0], pa.float64()),
        }))

        def slim_run(eng: BranchEngine):
            """The entire runner, SQLMesh-free: ~20 lines."""
            con = duckdb.connect()                     # ephemeral, in-memory
            produced: dict[str, pa.Table] = {}
            for t in topo_order(models):
                for dep in deps_of(models[t]):
                    if dep in produced:
                        arrow = produced[dep]
                    else:                              # branch-resolved Iceberg read
                        snap = eng.resolve_read(dep)
                        arrow = eng.catalog.load_table(dep).scan(
                            snapshot_id=snap).to_arrow()
                    schema, name = dep.rsplit(".", 1)
                    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                    con.register(f"_v_{schema}_{name}", arrow)
                    con.execute(f'CREATE OR REPLACE VIEW "{schema}"."{name}" '
                                f'AS SELECT * FROM _v_{schema}_{name}')
                out = con.execute(models[t]).to_arrow_table()
                produced[t] = out
                if eng.current() is None or t in eng.current().scope \
                        or eng.current().open_scope:
                    if eng.current() is not None and t not in eng.current().scope:
                        eng.grow_scope(t)
                    eng.write(t, out, mode="overwrite")
            con.close()
            return produced

        slim_run(eng)                                  # main baseline
        main_totals = eng.catalog.load_table("demo.totals").scan().to_arrow()
        assert main_totals.to_pydict()["total_amount"] == [30.0]

        eng.create("slim", [], open_scope=True)        # branch-first, even
        models["demo.orders_clean"] = models["demo.orders_clean"].replace(
            "> 0", ">= -100")
        # prod ingests after the branch point; epoch must hide it
        eng.switch("main")
        eng.write("raw.orders", pa.table({
            "id": pa.array([9], pa.int64()),
            "amount": pa.array([900.0], pa.float64())}))
        eng.switch("slim")
        slim_run(eng)

        b = eng.catalog.load_table("demo.totals")
        branch_total = b.scan(
            snapshot_id=b.metadata.refs["slim"].snapshot_id
        ).to_arrow().to_pydict()["total_amount"]
        main_total = b.scan().to_arrow().to_pydict()["total_amount"]
        ok = branch_total == [25.0] and main_total == [30.0]
        report("5. slim runner: Iceberg views -> DuckDB -> branch ref, "
               "epoch held, main untouched", ok,
               f"branch={branch_total}, main={main_total}")
        shutil.rmtree(proj, ignore_errors=True)
    except Exception as e:
        report("5. slim runner end-to-end", False, repr(e))

    print("\n--- SUMMARY ---")
    for status, step in RESULTS:
        print(f"  {status}: {step}")
    return 0 if all(s == "PASS" for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
