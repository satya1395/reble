# Changelog

## 0.6.0

- **Spark engine is real.** `compute_policy.prefer: spark` (or
  `reble run --engine spark`) executes the same contract as DuckDB —
  scoped runs, tag-pinned inputs via `VERSION AS OF` snapshot reads,
  provenance in the snapshot summary, branch writes, fast-forward
  promotion. Embedded PySpark (`local[*]` by default), `reble[spark]`
  extra. Spark and pyiceberg share one catalog backend (Glue, Hive,
  REST, or sql-over-Postgres) so refs are visible to both. See
  [Engines](https://satya1395.github.io/reble/engines/).
- Diffs remain on DuckDB regardless of engine (read-only compute;
  documented as an honest limit).

## 0.5.1

- **Fix: `region` in reble.yml now works for Glue catalogs.** pyiceberg
  expects `glue.region`; a plain `region` key was silently ignored and runs
  failed with a region-lookup error. Reble now translates the key, so the
  documented config works as written.
- AWS tutorial rewritten from zero: Python version prerequisites, install
  version-checking (`reble[aws]==0.5.1`), bucket setup, seed script, full
  walkthrough, and troubleshooting for every failure mode hit in real
  testing.

## 0.5.0

- **State lives in SQL, not a JSON file.** SQLite locally (default,
  `.reble/state.db`), Postgres for shared/CI state (`state.backend:
  postgres`). Config is validated before any run; legacy `state.json` is
  migrated automatically.
- **MCP server** (`reble mcp`): 9 tools covering the full verb surface,
  agent change-set protocol, structured error codes.
- AWS smoke test packaged (`python -m reble.aws_smoke`) — full lifecycle on
  disposable Glue+S3 resources, asserts zero `iceberg_scan` fallbacks,
  `--show-policy` prints the IAM policy.

## 0.4.0

- **Runs on AWS**: Glue catalog + S3 warehouse, verified end-to-end by a
  self-cleaning smoke (`examples/aws-glue/smoke.py`) that asserts streaming
  reads engage over S3 — measured 1M-row lifecycle on real S3 with zero
  fallbacks. AWS quickstart in the docs.
- `reble[aws]` extra (`pyiceberg[glue]`).
- duckdb S3 credentials auto-configure from boto3's default chain
  (AWS_PROFILE / env / SSO) when `engines.duckdb.settings` is unset;
  manual settings take full responsibility. Values never logged.
- `reble run --force`: rebuild the scope even when SQL hashes are
  unchanged (reruns replace, never append).
- **`kind: incremental` removed from the model contract.** Every run
  fully rebuilds its scope, so the word was a lie; builds now reject it.
  It returns when watermark / insert-overwrite execution is real (roadmap
  0.5), alongside date-range backfills (0.6).

## 0.3.2

Docs release — no code changes.

- **Where-Reble-sits diagram** (README + docs home): models → Reble →
  compute → your catalog → your storage; schedulers and agents invoke
  verbs. Positioning sharpened: a transformation engine (dbt-core shape),
  not a scheduler — cron/Airflow decide when.
- **Airflow guide**: nightly refresh, refresh-on-ingest, task-per-model,
  gated promotion with exit-code semantics; provider package on the
  roadmap, BashOperator patterns first-class today.
- **CI-triggered refresh** workflow example in the README (scheduled +
  `workflow_dispatch` from ingestion).
- **Plain-SQL vocabulary for non-dbt teams**: "model" defined as *one SQL
  file that produces one table* up front (README, docs home, getting
  started, concepts), plus a mapping for teams that schedule SQLs via
  DAGs or internal webapps — files are models as-is, dependencies parse
  from the SQL, your scheduler stays.

## 0.3.1

- **`reble run --refresh`** — data-driven scope for nightly refreshes:
  rebuilds exactly the models whose upstream snapshots moved, closed
  downstream; `reble estimate --refresh` previews the same scope. Docs
  gain a Refreshes section (order from lineage, scope from movement,
  scheduling from cron).
- Fixed: running on `main` no longer disambiguates to a suffixed branch —
  nightly refreshes land on main.
- Fixed: `iceberg_scan` locations are inlined as literals — prepared
  parameters cannot be prepared inside `CREATE VIEW`, which had silently
  disabled the streaming read path in 0.3.0.
- `reble init` gitignores sql-catalog artifacts (catalog.db, warehouse/).

## 0.3.0

- **DuckDB read path at scale**: run inputs and diffs stream through
  `iceberg_scan(snapshot_from_id=…)` — out-of-core, spilling under
  `engines.duckdb.memory_limit` (default temp dir `.reble/spill`), with
  per-table fallback to in-memory reads. Correctness stays pinned to
  catalog-committed snapshot ids; identical results verified across modes
  at 5M rows.
- **`reble estimate`**: rough cost estimate from snapshot summaries —
  models, per-table rows/bytes, summed read bytes; nothing scanned.
- `engines.duckdb` config: `read_mode`, `memory_limit`, `temp_directory`,
  `settings` (raw SET passthrough, e.g. s3 credentials).

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
