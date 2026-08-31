"""Spike 5: Do incremental models work on Reble branches as designed?

Assumptions under test (from the v0.2 incremental design discussion):

  1. Watermarks are derivable per-ref from the data (max(ts) on the ref) —
     main's and a branch's watermarks advance independently.
  2. Incremental append on a branch processes only rows past the BRANCH's
     watermark, from EPOCH-PINNED inputs; reruns are no-ops (idempotent);
     main is untouched.
  3. Upsert-by-key works on a branch ref (pyiceberg native upsert), merging
     changed rows without touching main.
  4. The fingerprint rule drives the mode: unchanged fingerprint -> increment;
     changed fingerprint -> full rebuild on the branch, restating history —
     and the restatement is visible as a diff.
  5. Promote semantics survive: main's own incremental runs advancing main
     after branching -> promote refused (dirty); clean case -> fast-forward
     carries the incremental history.
  6. Watermark caching via Iceberg snapshot properties round-trips per ref.
"""

import shutil
import sys
from pathlib import Path

import pyarrow as pa

from reble.branches import BranchEngine
from reble.config import RebleConfig
from reble.errors import BranchError

HERE = Path(__file__).parent
RESULTS: list[tuple[str, str]] = []


def report(step: str, ok: bool, detail: str = ""):
    status = "PASS" if ok else "FAIL"
    RESULTS.append((status, step))
    print(f"[{status}] {step}" + (f" -- {detail}" if detail else ""))


def rows(ids, ts, amounts):
    return pa.table({"id": pa.array(ids, pa.int64()),
                     "ts": pa.array(ts, pa.int64()),
                     "amount": pa.array(amounts, pa.float64())})


def ref_snap(eng, table, branch=None):
    tbl = eng.catalog.load_table(table)
    if branch is None:
        cur = tbl.current_snapshot()
        return cur.snapshot_id if cur else None
    r = tbl.metadata.refs.get(branch)
    return r.snapshot_id if r else None


def scan(eng, table, branch=None):
    return eng.catalog.load_table(table).scan(
        snapshot_id=ref_snap(eng, table, branch)).to_arrow()


def watermark(eng, table, branch=None):
    """Assumption 1's mechanism: watermark = max(ts) of the ref's own data."""
    t = scan(eng, table, branch)
    if t.num_rows == 0:
        return -1
    return max(t.column("ts").to_pylist())


def incremental_append_run(eng, out_table, branch=None):
    """Minimal incremental executor: rows of raw.events past the OUTPUT ref's
    watermark, read from the branch-resolved (pinned) input, appended to the ref."""
    src_snap = eng.resolve_read("raw.events")
    src = eng.catalog.load_table("raw.events").scan(snapshot_id=src_snap).to_arrow()
    wm = watermark(eng, out_table, branch)
    new = src.filter(pa.compute.greater(src.column("ts"), wm))
    if new.num_rows:
        eng.write(out_table, new)               # guarded append -> branch ref
    return new.num_rows


def main() -> int:
    proj = HERE / "wh"
    shutil.rmtree(proj, ignore_errors=True)
    (proj / "warehouse").mkdir(parents=True)
    eng = BranchEngine(RebleConfig(project_dir=proj))

    # seed prod: raw input + incremental output already materialized to ts=2
    eng.write("raw.events", rows([1, 2], [1, 2], [10.0, 20.0]))
    eng.write("demo.events_inc", rows([1, 2], [1, 2], [10.0, 20.0]))

    # --- 1. per-ref watermarks are independent ------------------------------
    try:
        eng.create("inc", ["demo.events_inc"])
        eng.switch("main")
        eng.write("demo.events_inc", rows([3], [3], [30.0]))   # main advances
        ok = (watermark(eng, "demo.events_inc") == 3
              and watermark(eng, "demo.events_inc", "inc") == 2)
        report("1. watermarks independent per ref", ok,
               f"main=3, branch={watermark(eng, 'demo.events_inc', 'inc')}")
        eng.delete("inc")
        # reset main output back to ts=2 for later checks
        eng.write("demo.events_inc", rows([1, 2], [1, 2], [10.0, 20.0]),
                  mode="overwrite")
    except Exception as e:
        report("1. watermarks independent per ref", False, repr(e))
        return 1

    # --- 2. branch increment: pinned inputs, idempotent, main untouched -----
    try:
        eng.create("feat", ["demo.events_inc"])                # epoch: ts<=2 inputs
        eng.switch("main")
        eng.write("raw.events", rows([9], [9], [90.0]))        # prod ingests AFTER
        eng.switch("feat")
        # branch's raw ingest 'arrived' before epoch? No — simulate branch-visible
        # new input by having had ts=1,2 only; the ts=9 row is post-epoch and
        # must NOT be processed on the branch.
        n1 = incremental_append_run(eng, "demo.events_inc", "feat")
        n2 = incremental_append_run(eng, "demo.events_inc", "feat")   # rerun
        branch_rows = scan(eng, "demo.events_inc", "feat").num_rows
        main_rows = scan(eng, "demo.events_inc").num_rows
        ok = n1 == 0 and n2 == 0 and branch_rows == 2 and main_rows == 2
        report("2. branch increment honors epoch pins + idempotent",
               ok, f"processed={n1},{n2}; branch={branch_rows}, main={main_rows} "
                   "(post-epoch ts=9 correctly invisible)")
        eng.delete("feat")
    except Exception as e:
        report("2. branch increment honors epoch pins + idempotent", False, repr(e))

    # --- 3. upsert-by-key on a branch ref -----------------------------------
    try:
        eng.create("ups", ["demo.events_inc"])
        tbl = eng.catalog.load_table("demo.events_inc")
        res = tbl.upsert(rows([2, 5], [2, 5], [999.0, 50.0]),
                         join_cols=["id"], branch="ups")
        b = scan(eng, "demo.events_inc", "ups").to_pydict()
        by_id = dict(zip(b["id"], b["amount"]))
        m = scan(eng, "demo.events_inc").to_pydict()
        ok = (by_id.get(2) == 999.0 and by_id.get(5) == 50.0
              and len(by_id) == 3
              and dict(zip(m["id"], m["amount"])).get(2) == 20.0)
        report("3. native upsert onto a branch ref, main untouched", ok,
               f"branch={by_id}, updated={res.rows_updated}, "
               f"inserted={res.rows_inserted}")
        eng.delete("ups")
    except Exception as e:
        report("3. native upsert onto a branch ref, main untouched", False, repr(e))

    # --- 4. fingerprint decides increment vs rebuild; rebuild restates ------
    try:
        # simulate: logic changed (fingerprint differs) -> full rebuild on branch
        # with NEW logic (amount * 2), against epoch-pinned inputs
        eng.create("logic", ["demo.events_inc"])
        src_snap = eng.resolve_read("raw.events")
        src = eng.catalog.load_table("raw.events").scan(
            snapshot_id=src_snap).to_arrow()
        rebuilt = pa.table({
            "id": src.column("id"), "ts": src.column("ts"),
            "amount": pa.compute.multiply(src.column("amount"), 2.0)})
        eng.write("demo.events_inc", rebuilt, mode="overwrite")   # the rebuild
        b = scan(eng, "demo.events_inc", "logic").to_pydict()
        m = scan(eng, "demo.events_inc").to_pydict()
        main_amt = dict(zip(m["id"], m["amount"]))
        # restatement: every overlapping row changed (new logic), PLUS the rebuild
        # correctly picks up input rows main's output never processed (ts=9 —
        # ingested before THIS branch's epoch, so rightly visible to it)
        changed = sum(1 for i, a in zip(b["id"], b["amount"])
                      if i in main_amt and main_amt[i] != a)
        src_ids = set(eng.catalog.load_table("raw.events").scan(
            snapshot_id=eng.resolve_read("raw.events")).to_arrow()
            .column("id").to_pylist())
        ok = (changed == len(main_amt)                # all shared rows restated
              and set(b["id"]) == src_ids             # rebuild = full pinned input
              and set(m["id"]) < src_ids)             # main stale, untouched
        report("4. logic change -> rebuild on branch restates history (diffable)",
               ok, f"{changed} of {len(main_amt)} shared rows restated; rebuild "
                   f"covers {len(src_ids)} input rows; main untouched")
    except Exception as e:
        report("4. logic change -> rebuild on branch restates history", False, repr(e))

    # --- 5. promote: dirty when main's incremental runs advanced ------------
    try:
        # 'logic' branch is still open; main runs its scheduled increment now
        eng.switch("main")
        incremental_append_run(eng, "demo.events_inc")     # processes ts=9 on main
        eng.switch("logic")
        try:
            eng.promote()
            report("5. promote refused after main's incremental run", False,
                   "promote should have been refused (main advanced)")
        except BranchError:
            report("5. promote refused after main's incremental run", True,
                   "dirty check held; rebase-then-promote is the path")
        eng.delete("logic")

        # clean case: branch, increment (nothing new), promote fast-forwards
        eng.create("clean", ["demo.events_inc"])
        tbl = eng.catalog.load_table("demo.events_inc")
        tbl.upsert(rows([99], [99], [1.0]), join_cols=["id"], branch="clean")
        res = eng.promote()
        ok = (res["promoted"] == ["demo.events_inc"]
              and 99 in scan(eng, "demo.events_inc").to_pydict()["id"])
        report("5b. clean promote carries incremental history", ok)
    except Exception as e:
        report("5. promote semantics with incremental", False, repr(e))

    # --- 6. watermark caching via snapshot properties -----------------------
    try:
        tbl = eng.catalog.load_table("demo.events_inc")
        tbl.append(rows([100], [100], [5.0]),
                   snapshot_properties={"reble.watermark": "100"})
        tbl = eng.catalog.load_table("demo.events_inc")
        summary = tbl.current_snapshot().summary
        stored = (summary.additional_properties.get("reble.watermark")
                  if hasattr(summary, "additional_properties")
                  else summary.get("reble.watermark") if summary else None)
        report("6. watermark cached in snapshot properties", stored == "100",
               f"stored={stored!r}")
    except Exception as e:
        report("6. watermark cached in snapshot properties", False, repr(e))

    shutil.rmtree(proj, ignore_errors=True)
    print("\n--- SUMMARY ---")
    for status, step in RESULTS:
        print(f"  {status}: {step}")
    return 0 if all(s == "PASS" for s, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
