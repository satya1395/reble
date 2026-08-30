"""Spike 3: Can SQLMesh be driven entirely from the Python API, the way Reble needs?

Tests, in order:
  1. Load a project via Context (no CLI)
  2. plan/apply to prod programmatically, no prompts
  3. Query materialized results back as Arrow (the hand-off to pyiceberg)
  4. Create an isolated dev environment programmatically (maps to a reble branch)
  5. Column-level lineage via SQLMesh's own API (no SQL parsing of ours)
  6. Change detection: touch one model, verify the plan sees only that change
  7. Close the loop: commit a model's output to an Iceberg *branch* via pyiceberg
     (the full Reble pipeline in miniature: SQLMesh plan -> DuckDB compute ->
      Iceberg branch ref)
"""

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent
PROJECT = HERE / "project"
RESULTS: list[tuple[str, str]] = []


def report(step: str, ok: bool, detail: str = ""):
    status = "PASS" if ok else "FAIL"
    RESULTS.append((status, step))
    print(f"[{status}] {step}" + (f" -- {detail}" if detail else ""))


def main() -> int:
    # clean slate
    for p in [PROJECT / "db.db", PROJECT / ".cache"]:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    # --- 1. Load project via Python API ---
    try:
        from sqlmesh.core.context import Context
        ctx = Context(paths=[str(PROJECT)])
        models = sorted(m.name for m in ctx.models.values())
        report("1. Context loads project", len(models) == 2, f"models={models}")
    except Exception as e:
        report("1. Context loads project", False, repr(e))
        return 1

    # --- 2. plan/apply to prod, no prompts ---
    try:
        plan = ctx.plan(auto_apply=True, no_prompts=True)
        report("2. plan/apply to prod programmatically", True,
               f"new snapshots={len(plan.new_snapshots)}")
    except Exception as e:
        report("2. plan/apply to prod programmatically", False, repr(e))
        return 1

    # --- 3. Read results back as Arrow ---
    try:
        df = ctx.fetchdf("SELECT * FROM demo.customer_totals ORDER BY customer")
        import pyarrow as pa
        arrow_out = pa.Table.from_pandas(df)
        ok = arrow_out.num_rows == 2 and "total_amount" in arrow_out.column_names
        report("3. Fetch materialized output as Arrow", ok,
               f"rows={arrow_out.num_rows}, cols={arrow_out.column_names}")
    except Exception as e:
        report("3. Fetch materialized output as Arrow", False, repr(e))
        arrow_out = None

    # --- 4. Isolated dev environment (the SQLMesh half of a reble branch) ---
    try:
        ctx.plan(environment="dev", auto_apply=True, no_prompts=True,
                 include_unmodified=True)
        df = ctx.fetchdf("SELECT count(*) AS n FROM demo__dev.customer_totals")
        report("4. Programmatic dev environment", int(df["n"][0]) == 2,
               "demo__dev views exist and query cleanly")
    except Exception as e:
        report("4. Programmatic dev environment", False, repr(e))

    # --- 5. Column-level lineage from SQLMesh's API ---
    try:
        from sqlmesh.core.lineage import lineage
        node = lineage("total_amount", ctx.get_model("demo.customer_totals"))
        upstream = [d.name for d in node.walk()][1:]   # skip the column itself
        ok = any("amount" in u for u in upstream)      # traces to orders.amount
        report("5. Column-level lineage API", ok,
               f"total_amount <- {upstream}")
    except Exception as e:
        report("5. Column-level lineage API", False, repr(e))

    # --- 6. Change detection: touch one model, plan sees exactly one change ---
    try:
        model_file = PROJECT / "models" / "customer_totals.sql"
        original = model_file.read_text()
        model_file.write_text(original.replace(
            "SUM(amount) AS total_amount",
            "SUM(amount) AS total_amount,\n  AVG(amount) AS avg_amount"))
        ctx2 = Context(paths=[str(PROJECT)])
        plan2 = ctx2.plan(no_prompts=True, auto_apply=False)
        changed = [s.name for s in plan2.new_snapshots]
        ok = any("customer_totals" in c for c in changed) and \
             not any("orders" in c and "customer" not in c for c in changed)
        report("6. Change detection: only the edited model is in the plan", ok,
               f"changed={changed}")
        model_file.write_text(original)  # restore
    except Exception as e:
        report("6. Change detection: only the edited model is in the plan", False, repr(e))

    # --- 7. Full loop: SQLMesh output -> Iceberg branch ref via pyiceberg ---
    try:
        from pyiceberg.catalog import load_catalog
        wh = HERE / "iceberg_wh"
        shutil.rmtree(wh, ignore_errors=True)
        wh.mkdir()
        cat = load_catalog("local", type="sql",
                           uri=f"sqlite:///{wh}/cat.db", warehouse=f"file://{wh}")
        cat.create_namespace("demo")
        tbl = cat.create_table("demo.customer_totals", schema=arrow_out.schema)
        tbl.append(arrow_out)                                    # main
        s1 = tbl.current_snapshot().snapshot_id
        tbl.manage_snapshots().create_branch(s1, "pr-1").commit()
        tbl.append(arrow_out, branch="pr-1")                     # branch write
        tbl = cat.load_table("demo.customer_totals")
        main_n = tbl.scan().to_arrow().num_rows
        br_n = tbl.scan(
            snapshot_id=tbl.metadata.refs["pr-1"].snapshot_id).to_arrow().num_rows
        report("7. SQLMesh output committed to an Iceberg branch",
               main_n == 2 and br_n == 4,
               f"main={main_n} rows, pr-1={br_n} rows")
    except Exception as e:
        report("7. SQLMesh output committed to an Iceberg branch", False, repr(e))

    print("\n--- SUMMARY ---")
    for status, step in RESULTS:
        print(f"  {status}: {step}")
    return 0 if all(s == "PASS" for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
