# Command reference

Every command accepts the global flags `--config PATH`, `--profile NAME`,
`--json`, `--no-color`, `--quiet`. Machine consumers: `--json` emits the
stable envelope (SPEC §6), and `run`/`diff` accept `--events` for versioned
NDJSON progress. Exit codes are a contract (SPEC §8) — the common ones are
noted per command.

Normative detail, including the `reble.yml` schema and envelope shape, is in
[`SPEC.md`](https://github.com/satya1395/reble/blob/main/SPEC.md).

## reble init

Write `reble.yml`, gitignore `.reble/`, probe catalog connectivity, detect
`models/`.

```bash
reble init --catalog sql --namespace analytics
```

`--catalog` `glue|polaris|nessie|hive|rest|reble|sql|in-memory` ·
`--engine` `duckdb|spark` · `--namespace` · `--state local|postgres` ·
`--yes`

For shared state (multiple workers, CI runners, Airflow):

```yaml
state:
  store: postgres
  uri: ${REBLE_STATE_URI}
```

Requires `pip install 'reble[postgres]'`; validated at startup (exit 2
if unreachable).

Exits `2` if the catalog is unreachable.

## reble run

The main verb: resolve scope, create/update the data branch, pin inputs,
execute.

```bash
reble run                        # scope inferred from your edits
reble run --models stg,mart      # explicit scope
reble run --refresh              # data-driven scope: upstream snapshots moved
reble run --force                # rebuild even when SQL is unchanged
reble run --depth 2              # cap the downstream cascade
reble run --dry-run              # preflight only, writes nothing
```

`--change-set ID` keys the work (else git branch, else `REBLE_CHANGE_SET`,
else the `local` default). `--branch NAME` resumes an existing data branch
under this change-set. Idempotent per model; a model skips only when its
SQL is unchanged *and* no in-scope parent ran. Incremental models always
full-refresh on branches — announced, never silent. Exits `5` only via the
verbs that treat empty scope as an error; an empty-scope `run` is legal and
registers the branch.

## reble diff

Row-level + schema diff of the branch's scope tables.

```bash
reble diff                       # whole scope
reble diff mart_orders           # specific tables
reble diff --against main        # advisory "what would promote do"
reble diff --schema-only
reble diff --rows 50             # save 50 sample rows per category
reble diff --full                # save all changed rows
```

The terminal shows one line per table: `+added / -removed / ~changed`
counts and the schema delta. Sample rows are never dumped to the terminal —
they are saved as JSON under `.reble/diffs/<change-set>/` (one file per
table plus `summary.json`), refreshed on each diff. `--rows` caps how much
detail is saved (default: `diff.max_rows_dumped`, 1000). Diff keys come
from the model's `-- key:` header or `diff.keys` in config. Exits `7` when
a table has no key and `on_missing_key: error`.

## reble status

The "where was I?" answer — read-only, CI-safe.

```bash
reble status --json
```

Sections: un-run code changes, branch scope, drifted pins, age/expiry.
Exits `3` when drift is detected — point CI at it.

## reble promote

Accept the change: per-table fast-forwards of main to the branch heads.

```bash
reble promote --dry-run
reble promote --ff-only    # refuse instead of re-running under drift
```

Fast-forward is legal only when every pinned input still equals main;
otherwise Reble re-pins, re-runs the scope, emits the authoritative
promote-time diff, then fast-forwards. Re-entrant: an interrupted promote
resumes without double-applying. Exits `4` when blocked (`--ff-only` under
drift, or a failed re-run).

## reble branch

```bash
reble branch create <name> [--from REF] [--change-set ID]
reble branch list
reble branch show <name>
reble branch discard <name> [--yes]
```

Explicit branch management — required when `git_sync: false`. `discard`
drops branch refs and pin tags, and refuses while a promote is in progress.

## reble gc

Expire TTL'd branches, drop orphan pin tags.

```bash
reble gc --dry-run
reble gc --before 7
```

Orphan pin tags block snapshot expiration on production tables — this is a
correctness command, not hygiene.

## reble estimate

Rough cost estimate before running — from Iceberg snapshot summaries only,
nothing scanned.

```bash
reble estimate                  # scope inferred from your edits
reble estimate --models stg     # explicit scope
reble estimate --refresh        # data-driven scope preview
```

Models to run, per-table rows/bytes for scope tables and pinned inputs,
summed bytes a run+diff will read. Warns about its own roughness by design
— accurate estimation is deliberately not a goal.

## reble mcp

Run the MCP server (stdio) — the same verbs as tools for AI agents.
Requires `pip install 'reble[mcp]'`; equivalent to the `reble-mcp` console
script. Configured by the host with `REBLE_PROJECT_DIR` and optional
`REBLE_PROFILE`.
