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

Until then, every run rebuilds its models from scratch — which is
precisely why reruns give the same answer and diffs can be trusted.

## If something fails halfway, just run it again

Runs remember what already finished. If a run dies in the middle of fifty
models, running the same command again picks up where it left off —
finished models aren't redone, and nothing gets duplicated. The same
applies to promotion: interrupt it, run it again, done.

**"Retry" is just "run the same command again."** That's also why your
scheduler's normal retry settings need no special handling — Airflow
retries a Reble task like any other task.

`reble run --force` means "redo everything in scope, even though nothing
changed" — useful when you've switched compute engines. Forcing never
duplicates rows: a rerun always *replaces* the previous result.

## Backfills are branch-shaped today, date-shaped soon

Today: branch, rebuild the affected models over tag-pinned inputs
(deterministic reproduction of the past state), diff, promote — the
scenario from [Days you've had](scenarios.md).

What does *not* exist yet: backfilling a date range without recomputing
the whole table. Partition-scoped rebuilds are the roadmap item, and
where Reble intends to beat dbt's `var('start')` pattern with branch +
partition insert-overwrite.
