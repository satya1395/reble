# Changelog

## 0.2.1

- Rewritten README pitch: concrete what/why/loop for data engineers.
- Docs site (mkdocs-material) at https://satya1395.github.io/reble/ —
  getting started, concepts (incl. the git↔Reble mapping and the no-merge
  argument), comparisons vs lakeFS / Nessie / warehouse clones / dbt,
  command reference, and scenarios.

## 0.2.0

Complete rewrite of the 0.0.x/0.1.x line. The model contract, semantics, and
surfaces below are normative in [SPEC.md](SPEC.md); behavior decisions are
recorded in [DECISIONS.md](DECISIONS.md).

- **Plain-SQL model contract** — `models/**/*.sql`, one file = one model,
  semantics in a `-- model/kind/key` header. No orchestrator dependency;
  dbt may return later as a runner adapter, never a core dependency.
- **SQLGlot lineage** — registry-matched table references are edges;
  everything else is an upstream input pinned with an Iceberg tag at run
  time. Cosmetic edits hash identically and never trigger a run.
- **Scoped branching on native Iceberg refs** — zero-copy branch refs on any
  compliant catalog (Glue, Polaris, Nessie, Hive, REST, local sql).
- **Row-level diff engine** (DuckDB) — added/removed/changed counts, samples,
  schema deltas; diff keys from the `key:` header or config.
- **Fast-forward-only promote** — per-table with re-entrant state; drift
  forces a scoped re-run and an authoritative promote-time diff. No
  three-way merges, ever.
- **Change-set keying** — `--change-set` / `REBLE_CHANGE_SET` / git
  derivation; `--branch` resumes an existing branch; `local` default for
  git-less projects.
- **Event streams** — `--events` NDJSON on run/diff; versioned schema.
- **Provenance** — `reble.*` snapshot-summary properties travel with
  snapshots to main; SQL bundles in run manifests.
- **MCP tool surface** — `pip install reble[mcp]`, `reble-mcp` (stdio);
  agents drive the same core verbs with structured error codes.
- **Headless core** — CLI and MCP are adapters over `reble.core.Reble`.

## 0.1.1 and earlier

Initial experimental line (superseded).
