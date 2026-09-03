---
title: "Glossary"
description: "Every term Reble's docs and output use, defined once."
---
One definition per term, with a link to the chapter that explains it.

**Advisory diff** — a diff taken before promotion. It shows what the change
does to the rows *as of the pinned inputs*. Compare **authoritative diff**.
→ [Branches and promotion](/reble/branches/#advisory-and-authoritative-diffs)

**AST hash** — a fingerprint of a model's SQL structure, not its text.
Reformatting, recasing, and comment edits produce the same hash and trigger
no work. → [Models and lineage](/reble/models/#how-edits-are-detected)

**Authoritative diff** — the diff Reble computes at promote time, after any
forced re-run. It is the one that matches what lands in production.
→ [Branches and promotion](/reble/branches/#advisory-and-authoritative-diffs)

**Base** — the ref a branch forks from, `main` by default. Configured as
`warehouse.default_base`. → [Configuration](/reble/config/#warehouse)

**Change-set** — the id Reble stores its state under, and the unit a data
branch is derived from. Comes from `--change-set`, `REBLE_CHANGE_SET`, or the
git branch. → [Running and scheduling](/reble/running/#how-work-is-keyed)

**Data branch** — the Iceberg branch refs Reble creates on each table in
scope, so a change is built in isolation from `main`. Metadata only; no data
is copied. → [Branches and promotion](/reble/branches/#a-data-branch)

**Diff key** — the column or columns that identify a row, declared as
`-- key:` in a model header. Without one, `reble diff` falls back to hashing
whole rows or errors, per `diff.on_missing_key`.
→ [Models and lineage](/reble/models/#the-header)

**Downstream closure** — everything built on top of a set of models. A model
whose inputs changed is itself changed, so the closure is always pulled into
scope. → [Models and lineage](/reble/models/#scope-what-a-run-rebuilds)

**Drift** — a pinned input no longer equals current `main`: production moved
since the branch was built. Reported by `reble status` as exit `3`. Not an
error. → [Branches and promotion](/reble/branches/#drift)

**Fast-forward** — moving `main` to the branch head when the branch was built
from current data. One metadata write per table, and the only way a change
reaches production. → [Branches and promotion](/reble/branches/#promote)

**Model** — one SQL file that produces one table. The only unit of work.
→ [Models and lineage](/reble/models/#a-model-is-one-sql-file)

**Pin** — an Iceberg tag recording the exact version of an upstream input at
the moment a branch first ran, so reruns are reproducible. Named
`reble_pin__<branch>__<table>`. → [Branches and promotion](/reble/branches/#pins)

**Promote** — apply a branch to production. Either a fast-forward or a scoped
re-run followed by one. Never a merge.
→ [Branches and promotion](/reble/branches/#promote)

**Provenance** — the `reble.*` properties written into every snapshot
summary: which model, which SQL hash, which run. Answers "what produced this
table state" from the catalog itself.
→ [Iceberg refs](/reble/iceberg-refs/#snapshot-summaries-the-provenance)

**Ref** — a named pointer to an Iceberg snapshot. Branches are movable refs;
tags are immutable ones. → [Iceberg refs](/reble/iceberg-refs/)

**Refresh** — `reble run --refresh`: a run whose scope comes from data
movement rather than SQL edits. The scheduled case.
→ [Running and scheduling](/reble/running/#three-ways-scope-is-decided)

**Run** — one execution of a scope: create or advance the branch, pin inputs,
execute models in dependency order.
→ [Running and scheduling](/reble/running/#what-a-run-does)

**Scope** — the set of models a run rebuilds: the seed models plus their
downstream closure. Computed, never declared.
→ [Models and lineage](/reble/models/#scope-what-a-run-rebuilds)

**Seed snapshot** — a zero-row snapshot Reble writes on `main` when a model's
table does not exist yet, because Iceberg cannot branch a table with no
snapshots. Main stays empty until promote.
→ [Iceberg refs](/reble/iceberg-refs/#branches-the-isolated-workspace)

**Snapshot** — an immutable, complete view of an Iceberg table at one commit.
Reading a snapshot gives the same rows forever.
→ [Iceberg refs](/reble/iceberg-refs/#what-an-iceberg-table-actually-is)

**Upstream input** — a table a model reads that is not itself a model,
typically filled by your ingestion. Reble never builds one; it pins them.
→ [Models and lineage](/reble/models/#lineage-comes-from-the-sql)
