# Concepts

One piece of vocabulary: a **model** is one SQL file that produces one
table — nothing more. Everything below builds on that.

## The problem, more precisely

Data teams can't safely try things. Four gaps compound:

1. **Environments are all-or-nothing.** A staging warehouse is a copy of
   *everything*, so it's expensive enough that you share it — and then you
   queue behind everyone else's experiments.
2. **Test inputs drift.** By the time your change finishes running against
   staging, prod has ingested three more hours of data. Your diff is against
   a world that no longer exists.
3. **"What rows does this change?" has no answer.** Code review sees the
   SQL; nobody sees the 300 rows the new filter silently drops.
4. **The modern stack almost had this.** Iceberg made branching *possible*
   (native branch refs, on any compliant catalog) — but nothing made it
   *usable*. You shouldn't need to run a new catalog server to get a branch.

Reble is the workflow layer on top of that last point: scoped branches,
deterministic inputs, row-level diffs, and a promote you can trust.

## Reble vs Git

If you know git, you already know Reble:

| Git | Reble |
| --- | --- |
| Repository | Iceberg catalog (the one you already run) |
| Branch | Data branch: zero-copy Iceberg refs, per changed table |
| Your working tree | `models/**/*.sql` — plain SQL files |
| `git diff` | `reble diff` — rows, not lines |
| `git status` | `reble status` — un-run edits, drifted pins |
| Commit | A run: scoped materialization on the branch |
| Merge (fast-forward) | `reble promote` |
| `.gitignore` | `.reble/` — machine-local state |
| Merge (three-way) | **Nothing. Deliberately absent.** |

### Where the analogy breaks: merge

Reble has no data merge, and never will. Promote is a **fast-forward** when
the branch's pinned inputs still equal production, and a **scoped re-run**
when they don't. That's the entire decision procedure — no conflict markers,
no resolution semantics, no "trust the tool."

This isn't a missing feature; it's the design. Three-way merges on data
silently corrupt warehouses: when two branches both derive from the same
base, "merging" their outputs requires guessing rows' intent. Refusing to
merge is also what keeps branches *cheap* — you never carry merge debt, so
branches can be short-lived and disposable.

The corollary: the diff you review before promote is **advisory**; the diff
Reble computes *at promote time* (after any forced re-run) is
**authoritative**. If main moved, you re-run and re-review — you never
review a stale diff and then get different rows.

## Scoped branching

A branch contains exactly the change's blast radius:

```
scope = edited models ∪ downstream closure
```

Edited models are detected by hashing the **canonical SQL AST** (via
SQLGlot), so cosmetic edits — whitespace, comments, keyword casing — never
trigger a run. Everything downstream of an edit is pulled in, because a
model whose inputs changed is itself changed. Cap the cascade with
`--depth N` if you want a tighter radius; the cut tables are reported as
stale.

A model may skip a run only when *both* its SQL is unchanged *and* none of
its in-scope parents executed. Unchanged SQL over changed inputs is a
changed table.

## Refreshes: order, scope, and scheduling without an orchestrator

"No orchestrator required" is three concrete claims:

**Order comes from lineage.** Every `reble run` executes models
topologically — parents strictly before children — from the same SQLGlot
graph that scopes branches. You never declare order, and you can't get it
wrong.

**Scope comes from movement.** Two kinds:

- *SQL changed* (the branch case): scope is inferred — edited models plus
  their downstream closure.
- *Data changed* (the nightly case): `reble run --refresh` scopes to
  exactly the models whose upstream snapshots moved since the model last
  built — an input ingested overnight makes its models stale, and the
  downstream closure joins them. On a quiet night it's an empty scope that
  costs one catalog listing. `--refresh` and `--models` are mutually
  exclusive; `reble estimate --refresh` previews the same scope.

The first-ever build declares itself (`--models all` or a list), because a
fresh project has no run history to infer from.

**State is configurable.** Locally it's SQLite — zero-config, works
everywhere. For teams sharing a warehouse from multiple workers (Airflow,
CI), one line switches to Postgres: `state.store: postgres` with a URI
in `reble.yml`. Concurrent workers on different change-sets never
conflict.

**Scheduling is deliberately yours.** Cron, GitHub Actions, Airflow — they
all just invoke one idempotent verb ([Airflow guide](airflow.md) for DAG
patterns). The *run* is the unit; *when* is not
Reble's layer. That means two patterns, and neither involves a human:

- *Scheduled*: a nightly line in crontab is a complete orchestration setup.

  ```bash
  0 3 * * * cd /srv/warehouse && reble run --refresh && reble gc
  ```

- *Event-triggered*: the job that lands your data calls the refresh when it
  finishes — an Airflow task after ingestion, a GitHub Actions
  `workflow_dispatch` from the pipeline, a Flink job's completion hook.
  Because `--refresh` scopes by snapshot movement, triggering it right
  after an ingest rebuilds exactly that ingest's blast radius; triggering
  it again later is a no-op. See the README's CI snippet for a runnable
  GitHub Actions example.

PRs are jobs too: `reble status` (exit 3 on drift) and `reble diff` make
cheap, meaningful checks in any CI system.

### Incremental, retries, and backfills — today's honest answers

**There is no `incremental` kind — deliberately.** Every run fully
rebuilds its scope: replace, never append. A kind that said "incremental"
while recomputing everything would be a lie in the contract, so the word
isn't in it. Watermark and partition-insert-overwrite execution are on the
[roadmap](https://github.com/satya1395/reble#roadmap-to-10) (0.5); the
kind returns when it means something.

**Retries are idempotent verbs.** Runs skip unchanged models (AST hashes)
and re-execute only what a change touches; promotion records per-table
re-entrant state. "Retry" is therefore *run the same command again* — it
resumes rather than duplicates, which is also why Airflow's plain task
retries work with no special handling. `reble run --force` rebuilds the
scope even when SQL is unchanged (a rerun *replaces* the previous output —
append-never, so forcing never duplicates).

**Backfill today is branch-shaped, not date-shaped.** The scenario from
[Days you've had](scenarios.md): branch, rebuild the affected models over
tag-pinned inputs (deterministic reproduction of the past state), diff,
promote. What does *not* exist yet: backfilling a date range without
recomputing the whole table — partition-scoped rebuilds are the roadmap
item, and where Reble intends to beat dbt's `var('start')` pattern with
branch + partition insert-overwrite.

## Pinning: why branches are deterministic

When you run on a branch, every **upstream input** (a table that isn't a
model you're branching) is pinned with an **Iceberg tag** at that moment.
Branch reads resolve through the tags — and tags block
`expire_snapshots`, so the pinned data physically cannot disappear while
the branch lives.

This is what makes a branch a *function of one input state* rather than a
snapshot of whatever prod happened to be mid-run. It's also the drift
detector: `reble status` compares each pin to the input's current main
snapshot, and any mismatch (exit code 3) means promote will force a re-run.

## Promote or discard. There is no third option.

```mermaid
flowchart LR
    D["reble diff<br/>(advisory)"] --> P{"reble promote"}
    P -->|"pins == main"| FF["fast-forward<br/>(per-table, resumable)"]
    P -->|"drift"| RR["re-pin + scoped re-run<br/>+ fresh authoritative diff"] --> FF
    P -.->|"never"| M["three-way merge"]
    style M stroke-dasharray: 5
```

Promotion is per-table fast-forwards on vanilla catalogs, with recorded,
re-entrant state — an interrupted promote resumes without re-executing
already-promoted tables. Promotion is also *provenance-carrying*: every
branch snapshot records the model name, AST hash, and run id that produced
it, and that metadata travels with the snapshot to main.

## Branches are ephemeral

Branches are cheap to create and meant to die young: promote them, or
`reble branch discard` them. TTL expiry (`reble gc`) drops stale branches
and — importantly — **orphan pin tags**, which otherwise block snapshot
expiration on production tables. GC in Reble is a correctness command, not
hygiene.

## Not git-coupled

Git is one way to say "this is my change-set." `reble run --change-set <id>`
(or `REBLE_CHANGE_SET`) works identically without a git repo — the default
key for git-less projects is simply `local`. This is the same surface CI
and AI agents use: every verb speaks a stable `--json` envelope with
documented exit codes, and `run`/`diff` emit versioned event streams.
