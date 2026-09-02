# Comparisons

The honest one-paragraph version of each, said the way we'd want it said
about us.

## Reble vs lakeFS

[lakeFS](https://lakefs.io) versions **objects**: branches are prefixes in
an object store, and tools see them as paths. That's infrastructure-level
versioning — but a lakehouse already has table-level versioning in Iceberg,
and a path-shaped abstraction can't know tables, schemas, or rows. Reble
works one level up: branches are Iceberg refs, diffs are row-level and
keyed, and the scope of a branch is derived from your SQL lineage. lakeFS
also resolves changes with merges; Reble deliberately doesn't merge at all.

**Pick lakeFS if** you need versioning of arbitrary objects and you're happy
running its server. **Pick Reble if** your tables are Iceberg and you want
branching as a workflow, not as infrastructure.

## Reble vs Nessie

[Nessie](https://projectnessie.org) is a **catalog** with git-like
versioning — a good one, and a thing you run (plus a database for it). Its
branching is catalog-wide and its merges are real three-way merges across
tables. Reble is not a catalog: it runs against whatever catalog you already
have and adds the *workflow* — scope inference from SQL, tag-pinned inputs,
row diffs, fast-forward-or-rerun promotion. If you already run Nessie,
Reble can use it as a catalog like any other.

**Pick Nessie if** you want a versioned catalog as infrastructure and are
taking on its operation. **Pick Reble if** you want branches without
operating anything new — and without merge semantics on your data.

## Reble vs warehouse zero-copy clones

Snowflake and BigQuery offer zero-copy clones: instant table copies inside
the platform. They're genuinely good — and genuinely locked: clones live and
die inside that warehouse, priced by its economics, and a clone of a schema
is not a *scoped* branch (you clone the blast radius you know, not the one
your lineage implies). Reble's branches are open Iceberg metadata in *your*
storage and catalog; the diff and promote workflow travels with them.

**Pick clones if** you're staying inside one warehouse forever. **Pick
Reble if** your tables are Iceberg in your bucket, or might be.

## Reble vs dbt

dbt is an **orchestrator**: it compiles and runs your SQL. Reble is not an
orchestrator — models are plain SQL files, executed by Reble's engine
(DuckDB today, Spark behind the same interface) on branches. The products
solve different problems and are not substitutes: dbt answers "how do I
build my warehouse," Reble answers "how do I change it safely." There's no
dbt dependency anywhere in Reble; a dbt project's SQL is a migration story
("point Reble at your models directory"), not a requirement.

## Summary

| | Reble | lakeFS | Nessie | Warehouse clones |
| --- | --- | --- | --- | --- |
| What it versions | Iceberg tables | Objects | Catalog state | Platform tables |
| Something to run | Nothing (your catalog) | lakeFS server | Nessie + database | Nothing (the warehouse) |
| Open storage | ✓ (yours) | ✓ | ✓ | ✗ (platform) |
| Scope of a branch | SQL-lineage-inferred | Prefix you name | Whole catalog | What you clone |
| Diff | Row-level, keyed | Object lists | — | Platform-dependent |
| Merge | Never (FF or re-run) | Three-way | Three-way | n/a |
