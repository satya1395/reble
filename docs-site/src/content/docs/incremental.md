---
title: "Incremental & retries"
description: "Today's honest answers on incremental execution, retries, backfills."
---
Three questions every serious user asks, answered honestly.

## There is no `incremental` kind — deliberately

Every run fully rebuilds its scope: replace, never append. A model kind
that said "incremental" while recomputing everything would be a lie in the
contract, so the word isn't in it. Watermark and partition-insert-
overwrite execution are on the
[roadmap](https://github.com/satya1395/reble#roadmap-to-10); the kind
returns when it means something.

Until then, full rebuild inside a branch is the honest primitive: it is
what makes reruns deterministic and diffs trustworthy.

## Retries are idempotent verbs

Runs skip unchanged models (AST hashes) and re-execute only what a change
touches; promotion records per-table re-entrant state. **"Retry" is
therefore "run the same command again"** — it resumes rather than
duplicates, which is also why Airflow's plain task retries work with no
special handling.

`reble run --force` rebuilds the full scope even when SQL is unchanged
(the engine-switch case). A rerun *replaces* the previous branch output —
forcing never duplicates rows.

## Backfills are branch-shaped today, date-shaped soon

Today: branch, rebuild the affected models over tag-pinned inputs
(deterministic reproduction of the past state), diff, promote — the
scenario from [Days you've had](scenarios.md).

What does *not* exist yet: backfilling a date range without recomputing
the whole table. Partition-scoped rebuilds are the roadmap item, and
where Reble intends to beat dbt's `var('start')` pattern with branch +
partition insert-overwrite.
