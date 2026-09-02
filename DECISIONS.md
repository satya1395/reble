# DECISIONS.md

Normative-adjacent decisions for v0. Where the spec said "pick a behavior and
write it down," it is written down here. Supersessions of earlier framing are
marked.

## 1. Model contract (plain SQL, no orchestrator)

**Supersedes** the dbt-first framing of the original product chat. The core
product never parses a dbt manifest.

- Models are plain SQL files: `models/**/*.sql` (configurable via
  `lineage.models_path`). One file = one model. File stem = model name.
- Semantics live in a minimal header comment block:
  - `-- model: <name>` — optional, defaults to file stem.
  - `-- kind: table | view | incremental` — optional, defaults to `table`.
  - `-- key: <col>[, <col>]` — diff key; required for `incremental`.
- Lineage: every model file is parsed with SQLGlot. A table reference whose
  name matches a registry model is a dependency edge. Any other table
  reference is an upstream input and gets pinned via an Iceberg tag
  (`reble_pin__<branch>__<table>`) at run time.
- Unparseable SQL → exit 6 (lineage unresolved). No partial-scope runs.

## 2. dbt is an adapter, never a dependency

dbt may exist later as a *runner adapter* (a way to execute models), but the
core must not assume it: no dbt manifest parsing, no dbt config, no dbt
imports. Positioning language reflects this: "works with your Iceberg
catalog — bring your own SQL," not dbt-framed language.

## 3. Execution semantics (DuckDB-first, ours end to end)

- v0 engine: DuckDB + pyiceberg. Every model executes as
  CTAS-then-overwrite on the branch ref.
- `kind: view` is *materialized* like `table` in v0 (computed and written at
  run time). Compute-on-read views are a v0.2 question.
- `incremental` models always full-refresh inside branches (spec rule); the
  output announces it, never silently.
- New models (no table on main yet): the engine creates the table with a
  schema inferred from the first CTAS result (pyarrow → Iceberg), then writes
  on the branch. Main sees an empty table until promote.
- Input substitution is AST-based: table references in the model SQL are
  rewritten to DuckDB views over the appropriate snapshot (branch ref for
  in-scope models, pin tag for upstream inputs, base otherwise). No string
  templating.
- Spark is a config-declared runner behind the same engine interface —
  stubbed in v0 (`compute_policy.prefer: spark` produces a clear error).

## 4. Edited-model detection

"Edited" means changed since the branch point, computed as the union of:

1. git diff vs merge-base with `main` (or `master`) — file contents hashed on
   the canonical AST, so only real SQL changes count (git_sync mode only;
   invariant 1: reble reads git, never runs it);
2. AST hash differing from the hashes stored in `.reble/state.json` from the
   last run on this data branch (covers non-git flows and files git can't
   see);
3. explicit `--models`.

The base git branch is tried as `main`, then `master`. Override via
`reble branch create` + explicit flows when needed.

## 5. Pin stability across runs

Input pins are created at the *first* run that observes them and are not
retargeted by later runs — reads inside the branch resolve as of the branch
epoch (invariant 5). The only retargeting path is the promote-with-drift
re-run, which re-pins to current main after announcing it.

## 6. Promote drift signals

Preflight compares two things per branch:

- every input pin's pinned snapshot vs the input's current main head;
- every scope table's recorded main head ("base head") vs its current main
  head (someone wrote to main inside the blast radius).

Any mismatch = drift. Clean → per-table fast-forwards with re-entrant state
in `.reble/promote.json`. Drift + `--ff-only` → exit 4. Drift without
`--ff-only` → announce, re-pin, re-run scope, emit the authoritative
promote-time diff, then fast-forward. Cross-table atomicity is a reble
catalog feature and is labeled as such in output.

## 7. Fast-forward legality is a lineage check

Fast-forward legality is decided on the snapshot **parent chain**: promote is
legal when the base head is an ancestor of (or equal to) the branch head.
Snapshot-id *ordering* must not be used — ids are not monotonic across
generators (pyiceberg's are timestamp-derived and can invert numerically
within a table). A diverged main (base head not on the branch's ancestry) is
refused — never merged (invariant 9). The authoritative drift check remains
the pin/base-head comparison in §6.

## 8. Dropped from the original spec framing

- `--select SELECTOR` (dbt-style selector) — removed with the dbt dependency.
  Use `--models` and `--depth`.
- `lineage.manifest_path`, `lineage.source` — replaced by
  `lineage.models_path`.
- `reble.yml` gains an optional `warehouse.namespace` (Iceberg namespace for
  model tables). Without it, model names are used as bare table identifiers.

## 9. v0 non-goals (unchanged)

Three-way data merges (never), a storage engine, a governance catalog, a BI
tool. `reble estimate` remains v0.2.
