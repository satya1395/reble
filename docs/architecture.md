# Reble: Architecture & Implementation Plan (v2)

**Scope:** A single CLI that gives a small data team a complete local-first analytics
platform — DuckDB + Iceberg + SQLMesh pre-wired — with **subset branching** of the
warehouse and **branch-per-PR CI** as the killer workflow.

This replaces the v1 plan. Key changes from v1:
- Branching is **subset branching** (branch only the tables you're changing; read
  everything else from prod, pinned), not full-catalog branching.
- No Unity Catalog, no Trino, no web UI, no merge semantics in v1.
- Honest timeline: ~12 weeks solo to 0.1, with validation spikes first.

---

## 1. Product Shape

```
$ reble init my-warehouse        # scaffold project (SQLMesh models, config, local catalog)
$ reble run                      # run models against main
$ reble branch create fix-orders --tables orders,order_totals
$ reble run                      # runs on branch: writes go to branched refs,
                                 # reads of other tables hit pinned prod snapshots
$ reble diff                     # schema + row-level diff of branched tables vs main
$ reble promote                  # apply the change to main; delete branch
```

And in CI (the flagship workflow):

```yaml
# .github/workflows/reble.yml
- uses: rebleio/reble-action@v1   # on PR: branch named pr-123, run changed models,
                                  # post diff + impact comment; on merge: promote;
                                  # on close: delete branch
```

Everything runs on a laptop with **zero services**: DuckDB embedded, Iceberg tables on
local filesystem, SQLite-backed catalog, SQLMesh in-process. The same project scales to
team mode (S3 + Postgres/REST catalog + CI runners) by changing config, not tools.

---

## 2. The Branch Model (core IP)

A branch is **metadata only** — no data is copied:

```
branch "pr-123":
  scope:                          # tables being changed (writable)
    analytics.orders        -> iceberg ref "pr-123"        (copy-on-write)
    analytics.order_totals  -> iceberg ref "pr-123"        (copy-on-write)
  pins:                           # everything else (read-only, stable)
    raw.events              -> snapshot 9931
    raw.customers           -> snapshot 4812
  created_at / created_by / ttl
```

- **Branched tables** use Iceberg's native per-table refs. Writes create new data
  files under the branch ref; the `main` ref never sees them.
- **Unbranched tables** are read through to prod but **pinned to the snapshot IDs at
  branch-creation time**, so test inputs are stable and diffs are reproducible even
  while prod keeps ingesting. Pinning is free (Iceberg time travel).
- **Scope inference:** `--tables` is explicit in v1. Fast-follow: infer scope from the
  SQLMesh plan (the changed models' output tables), so `reble branch create` needs no
  arguments in the common case.

**Promote ≠ merge.** v1 promote:
1. If the branched tables' `main` refs haven't advanced since branch creation →
   fast-forward `main` to the branch ref (cheap, atomic per table).
2. Otherwise → re-apply the SQLMesh plan against main (rerun the changed models).
   No data-level merge, ever. Last-write-wins data merges silently corrupt warehouses;
   we refuse to build that. Branches are ephemeral: create, test, promote or discard.

**SQLMesh mapping:** one reble branch ↔ one SQLMesh environment (1:1). SQLMesh handles
model-level change detection and virtual promotion *within* its managed models; reble
adds the physical layer underneath — branched refs, pinned upstreams, and coverage of
tables SQLMesh doesn't manage (raw/source tables, external writers).

**Concurrency & team workflow.** Multiple engineers work on branches simultaneously
(independent refs; requires the shared team-mode catalog — local SQLite is
single-user) and promote sequentially:

1. **Promotes are serialized** (promote lock / merge-queue semantics; maps 1:1 onto
   PRs merging one at a time in CI).
2. **Disjoint scopes:** after someone else promotes, your branch's promote runs a
   **lineage-aware staleness check** — if none of your models read the tables that
   advanced, fast-forward proceeds; if they do, promote requires a rebase.
3. **Overlapping scopes:** the clean/dirty check fails (your table's main ref moved)
   → fast-forward refused, rebase mandatory. Warn at branch *creation* when a table
   in scope is already branched by someone else.
4. **`reble branch rebase`** is a first-class command: re-pin upstreams to current
   main, rerun changed models (cheap via SQLMesh change detection), re-validate.
   Rebase-then-promote is the only path forward for a stale branch — never data merge.

---

## 3. Architecture

```
┌────────────────────────────────────────────────────────────┐
│  reble CLI (Python, one entrypoint)                        │
│  init · branch · run · query · diff · promote · gc · ci    │
├────────────────────────────────────────────────────────────┤
│  Branch Engine                                             │
│  branch manifests · snapshot pinning · write guards ·      │
│  promote (fast-forward / re-apply) · TTL + GC              │
├──────────────────────────┬─────────────────────────────────┤
│  Catalog Resolver        │  Runner                         │
│  wraps the underlying    │  SQLMesh plan/apply with        │
│  Iceberg catalog; every  │  branch context; DuckDB         │
│  table lookup resolves   │  computes, pyiceberg commits    │
│  through branch context  │  to the branch ref              │
├──────────────────────────┴─────────────────────────────────┤
│  Storage & catalog                                         │
│  local:  filesystem warehouse + pyiceberg SQL catalog      │
│          (SQLite)                                          │
│  team:   S3/MinIO + Postgres SQL catalog or existing       │
│          REST catalog                                      │
└────────────────────────────────────────────────────────────┘
```

### Load-bearing design decisions

**Language: Python.** SQLMesh and pyiceberg are Python libraries; wrapping them
in-process is the whole point. "Single binary" is a distribution concern, not a
language concern: v1 ships via `uv tool install reble` / pipx (which today feels
binary-like); a PyApp/shiv single-file build is a fast-follow.

**Compute: DuckDB computes, pyiceberg commits.** The DuckDB iceberg extension is
read-oriented; we do not depend on DuckDB writing Iceberg. The runner executes model
SQL in DuckDB (reading branch-resolved Iceberg scans), collects results as Arrow, and
commits to the branch ref via pyiceberg. This is the honest write path and it must be
validated in the week-1 spike (see Risks).

**Catalog resolution is in-process for local, a REST proxy for team mode.** Locally,
reble wraps the pyiceberg catalog object directly — no server needed. `reble catalog
serve` (fast-follow, not v1) exposes the same resolver as an Iceberg REST catalog
proxy so external engines (Trino, Spark, Snowflake) resolve branches transparently.
The proxy is the long-term wedge ("branch your existing lakehouse, no migration");
the in-process resolver is the same code without the HTTP layer.

**Write guards are non-negotiable.** From a branch context, any write to a table
outside the branch scope is refused with a hard error. One corrupted prod table ends
the project's credibility.

**GC from day one.** Pinned snapshots block Iceberg snapshot expiration on prod
tables. Branches carry a TTL (default 14 days); `reble gc` expires dead branches,
drops their refs, and releases pins. The CI action deletes branches on PR close.

---

## 4. What v1 deliberately excludes

- Web UI (SQLMesh ships its own UI; `reble ui` can shell out to it later)
- Unity Catalog, Trino, Spark integrations
- Data-level merge of branches
- Multi-tenant cloud service, auth/RBAC
- Scheduling/orchestration (Airflow etc.)
- dbt compatibility (SQLMesh only)

---

## 5. Timeline (~12 weeks solo)

### Phase 0 — Spikes & validation (weeks 1–2)
The plan is contingent on these. Do not scaffold the product first.

- [x] **Spike: pyiceberg branch refs. ✅ GREEN (2026-08-30).** All operations work on
      pyiceberg 0.11.1: create_branch, `append(branch=...)`, bidirectional isolation,
      pinned snapshot reads, promote via `set_current_snapshot(ref_name=...)`,
      remove_branch, DuckDB reads via Arrow. See
      `spikes/01-pyiceberg-branches/RESULTS.md`. Note: no native fast-forward — the
      clean/dirty promote check is ours to implement (as planned).
- [x] **Spike: compute-path performance. ✅ GREEN at full 10GB (2026-08-30).**
      140M rows / 10.22GB Arrow on an M4 Pro: branch create <10ms (size-independent),
      pinned full scan 4.0s, full diff 5.9s, peak RSS 12.3GB, bulk load ~3.5M rows/s.
      N3 is now measured, not extrapolated. See `spikes/02-perf/RESULTS.md`.
      Design rules: lineage-driven projection pushdown on every scan; diff on
      key+compared columns only; DuckDB memory_limit + temp spill; release Arrow refs
      promptly.
- [x] **Spike: SQLMesh embedding. ✅ GREEN (2026-08-30).** sqlmesh 0.236.1 drives
      fully from Python: `Context.plan(auto_apply, no_prompts)`, programmatic dev
      environments, `lineage()` column graphs, plan-native change detection, and
      Arrow hand-off to pyiceberg branch commits verified end-to-end. See
      `spikes/03-sqlmesh-embedding/RESULTS.md`. Write path confirmed: DuckDB
      computes → Arrow → pyiceberg commits; no engine-adapter surgery needed.
- [ ] **Validation: 15–20 interviews** with data engineers. Script: "How do you test
      pipeline changes today? What breaks? Would branch-per-PR with row-level diffs
      change your workflow?" Kill or reshape the project on this evidence.

**Gate:** all three spikes green + interviews confirm the pain → proceed.

### Phase 1 — Branch engine + CLI core (weeks 3–5)
- [ ] Project scaffold: `reble init` (config, SQLMesh project, local warehouse,
      SQLite catalog, seed example)
- [ ] Branch manifest store (SQLite locally; same schema on Postgres for team mode)
- [ ] Catalog resolver (in-process): scope → refs, pins → snapshots, guard writes
- [ ] `reble branch create|list|switch|delete` with `--tables` scoping and TTLs
- [ ] `reble query` / `reble shell`: DuckDB session with branch-resolved tables
- [ ] Integration tests: branch, write, verify isolation, verify pin stability

### Phase 2 — SQLMesh runner (weeks 6–8)
- [ ] `reble run`: SQLMesh plan/apply on the current branch (branch ↔ environment)
- [ ] Change detection surfaced: "3 models changed, 2 downstream affected" before run
- [ ] Column-level lineage exposed via `reble lineage <model>` (read from SQLMesh's
      APIs — no SQL parsing of our own)
- [ ] Scope inference: derive branch table scope from the SQLMesh plan

### Phase 3 — Diff + promote (weeks 9–10)
- [ ] `reble diff`: per branched table — schema diff, row counts, row-level
      added/removed/changed (DuckDB anti-joins over branch ref vs main ref; Iceberg
      changelog scan where available)
- [ ] **New-model mode:** a table with no main counterpart gets a *profile* instead
      of a diff — schema, row count, null rates, sample stats, upstream lineage.
      Modified models get diff + downstream impact; new models get profile + upstream
      deps. Both flow through the same CI comment (this is the audit step of WAP for
      greenfield work — pinned inputs + branch isolation matter here even though
      nothing is "changed").
- [ ] `reble promote`: fast-forward when clean, re-apply plan otherwise; release pins
- [ ] `reble gc`: TTL expiry, ref cleanup, pin release

### Phase 4 — CI workflow + release (weeks 11–12)
- [ ] `rebleio/reble-action`: PR open → branch `pr-N` + run + **PR comment** with
      impact summary (models changed, lineage-derived downstream impact, diff stats);
      merge → promote; close → delete
- [ ] Example project (e-commerce) exercised end-to-end in the action
- [ ] Docs: quickstart (< 5 min to first branch), CI setup guide, architecture
- [ ] Release 0.1.0: PyPI + GitHub release + announcement post

---

## 6. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| pyiceberg branch-ref writes immature | Medium | Week-1 spike; fallback to direct metadata commits; pin versions hard |
| DuckDB↔Iceberg read path too slow on pinned snapshots | Medium | Spike with 10GB; fallback: pyiceberg scan → Arrow → DuckDB |
| SQLMesh internals shift under us (fast-moving project) | Medium | Pin version; integrate via public Python API only. Governance risk is low since the March 2026 Linux Foundation donation (open community model, no single-vendor rug-pull). Upstream a change only if the public API can't support the branch integration |
| Pins block snapshot expiry → prod storage bloat | High if ignored | TTLs + `reble gc` in v1, CI auto-cleanup |
| Prod write from branch context | Low, catastrophic | Hard write guards + tests before any other feature |
| Interviews say SQLMesh envs already suffice | Real | That's why interviews are in Phase 0, before the build |

---

## 7. Success criteria for 0.1

1. `pip install` → first branched run in **under 5 minutes** on a laptop, no Docker.
2. Branch-per-PR action posts a diff comment on a real repo's PR.
3. Zero-copy verified: a branch of a 10GB table costs ~0 bytes until written.
4. Write guard: impossible to touch an out-of-scope table from a branch (tested).
5. 10 external users run it on their own models and come back with feedback.

---

## 8. After 0.1 (not commitments)

- `reble catalog serve` — REST catalog proxy → Trino/Spark/Snowflake branch access,
  and the "branch your existing lakehouse, no migration" wedge
- Single-file binary distribution (PyApp)
- Hosted branch/catalog service (the commercial layer, if adoption proves out)
