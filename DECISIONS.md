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

## 10. Change-set keying (v0.2; supersedes invariant 2's git-first framing)

The change-set id is the primary state key. Precedence: `--change-set` flag
→ `REBLE_CHANGE_SET` env → git branch (when `git_sync`) → the `local`
default when `git_sync: false` (standalone projects just work; `reble init`
writes `git_sync: false` when no git repo is present). Exactly one
change-set key drives a data branch per working copy; `--branch NAME` on any
verb resumes an existing data branch under the current change-set.

**Bootstrap rule:** a fresh change-set on a fresh data branch with no
`--models` gets an empty scope (invariant 6) — declare scope explicitly. The
hash baseline for edited detection falls back to the data branch's last run
manifest when the current key has no stored hashes, so a new change-set
resuming an existing branch stays incremental.

`BranchState.key_source` (git | explicit | env) records how the key was
derived — additive field, no migration needed.

## 11. Execution skip rule: inputs matter, not just SQL (v0.2 correctness fix)

A scope model may skip a run only when BOTH its AST hash is unchanged AND no
in-scope parent executed this run. The old rule (hash-only) wrongly skipped
downstream models whose inputs had just been recomputed — unchanged SQL over
changed inputs is a changed table.

## 12. Event streams: library callback first, NDJSON adapter second

The core calls an injected `on_event(name, **payload)`; the CLI `--events`
flag serializes to NDJSON on stdout with the final envelope printed as one
line (self-describing stream: event records carry `event`, the envelope
doesn't). The events schema (`events: "1"`) versions independently of the
envelope, additive-only within a major.

## 13. Provenance lives in snapshot summary properties

Not table properties — those aren't branch-scoped and would be clobbered
across branches. Summary props (`reble.model`, `reble.ast_hash`,
`reble.run_id`, `reble.changeset`, `reble.seed` on zero-row seeds) travel
with the snapshot, so promoted main snapshots keep their code lineage. Full
SQL text stays in run manifests to keep metadata small. Pin tags carry no
metadata (the Iceberg API has none); the run manifest is their record.

## 14. Promote resume advances base heads per table

The promoter updates `base_heads[table]` to the promoted snapshot (persisting
via callback) immediately after each per-table fast-forward. A resumed
partial promote therefore neither misreads stale heads as drift nor
re-executes already-promoted models.

## 15. Core-first: one library, two adapters

All orchestration lives in `reble/core.py` (`Reble` class); the CLI and the
MCP server parse flags/params, call a verb, and render the returned
envelope. Failures that still produce output raise `RebleError(payload=
<envelope>)`. The acceptance bar for any core change is that the CLI test
suite passes unchanged — the thin-client rule made testable.

## 16. Agent change-set protocol

`reble_run` (MCP) without `change_set` generates `mcp-<uuid8>` and returns
it top-level; agents thread it through status/diff/promote. Fresh
change-sets on fresh branches declare `models` explicitly (bootstrap rule,
§10); a new change-set on an existing branch (`--branch` / `branch`)
inherits the hash baseline from the branch's last run manifest.

## 17. Exit codes are a CLI concern; MCP gets structured errors

The spec exit-code table stays normative for process exits. MCP tools
return `{"ok": false, "error": {"code": <exit code>, ...}}` with any
partial envelope, because agents branch on structured fields, not shells.
Same codes, two encodings.

## 18. No Trino (for now): DuckDB read path at scale (supersedes the v1.5 Trino line)

Trino is dropped from the active roadmap. Rationale: it was only ever a read
path (diffs/status on big tables); writes at scale remain Spark's job. Scoped
branching keeps the diff working set small by design (blast radius, not
warehouse), and our real bottleneck is feeding DuckDB via full pyiceberg →
arrow materialization — not DuckDB, which has out-of-core execution. The
replacement increment: read run inputs and diffs through DuckDB's
`iceberg_scan()` (streams parquet from object storage, spills under
`memory_limit`, configurable via `engines.duckdb`), and make `reble estimate`
report diff bytes up front. Trino becomes a read adapter added on customer
pull — the engine interface already accommodates it.

## 19. Lazy reads via iceberg_scan; correctness pinned to snapshot ids

Run inputs and diffs stream through duckdb's `iceberg_scan(...,
snapshot_from_id=<id>)` (out-of-core, spilling under
`engines.duckdb.memory_limit`, default temp dir `.reble/spill`) instead of
materializing via pyiceberg → arrow. Per-table fallback to arrow on any
extension failure, surfaced as envelope warnings.

pyiceberg's UUID metadata filenames are inexpressible in duckdb-iceberg's
`version`/`version_name_format`, so the extension globs the latest metadata
under `unsafe_enable_version_guessing`. That flag is acceptable here and
only here: callers pin the **catalog-committed snapshot id** resolved from
refs/tags — snapshots are immutable and cumulative, so a pinned id reads
identically regardless of which metadata file enumerates it. Writes stay in
pyiceberg (branch refs + provenance snapshot-properties; duckdb-iceberg
writes would drop both).

Implementation note: the scan location must be inlined as a string literal
— `iceberg_scan(?)` as a prepared parameter cannot be prepared inside
`CREATE VIEW` (binder error; the first release silently fell back to arrow
because of this, caught by re-running the benchmark).

Measured (benchmarks/duckdb_scale.py, M-series laptop, sqlite+file
warehouse, lazy path verified engaged): branch create on a 5M-row table is
metadata-only (<10 ms); scoped run and keyed diff over 5M input rows
complete in ~0.7-0.9 s per pass in **both** read modes with **identical
diff results** — the lazy path's value is memory bounding (streams +
spills instead of whole-snapshot materialization, honoring `memory_limit`),
not wall time at laptop scale.

## 20. Refresh scope is data-driven; the default base is never disambiguated

`reble run --refresh` scopes to models whose upstream snapshots (input
tables and model dependencies, by main snapshot wall-clock timestamp) are
strictly newer than the model's own main snapshot, closed downward via the
same closure as edit-driven scope. No new recorded state: snapshot
timestamps are the signal. `--refresh` + `--models` is exit 2.

Corollary fix: the default base name is never name-disambiguated — running
on main (changeset "main") targets main itself, not a suffixed branch, or
nightly refreshes would never land on main.

## 21. Scheduling is not a layer

Order comes from lineage, scope from movement (SQL or data), and *when*
from cron/CI — the run is the unit. A scheduler inside Reble is refused for
the same reason merges are: it adds coordination surface without adding
correctness.
