---
title: "Scope"
description: "A branch contains exactly the change's blast radius."
---
A branch contains exactly the change's blast radius — never the whole
warehouse:

```
scope = edited models ∪ downstream closure
```

## How edits are detected

Reble reads the *structure* of your SQL, not its text. Reformat the
file, fix a comment, change the casing — nothing happens, because
structurally nothing changed. Add a filter, a join, a column — that model
enters the scope.

Everything downstream of an edit is pulled in, because a model whose
inputs changed is itself changed. Cap the cascade with `--depth N` if you
want a tighter radius; the tables cut off by the cap are reported as
stale.

## When a model skips

A model skips a run only when *both* hold:

1. its SQL is unchanged, **and**
2. none of its in-scope parents executed.

Unchanged SQL over changed inputs is a changed table — the second
condition is what makes reruns correct rather than merely fast.

## The first-ever build

A fresh project has no run history to infer from, so the first build
declares itself: `reble run --models all` (or a list). Every later run is
inferred from edits or data movement ([refreshes](refreshes.md)).
