---
title: "Git, optional"
description: "The mapping when you use git — and the standalone mode when you don't."
---
Git is one way to say "this is my change-set." It is not a requirement.

## The mapping, if you know git

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
when they don't. That's the entire decision procedure — no conflict
markers, no resolution semantics, no "trust the tool."

This isn't a missing feature; it's the design. Three-way merges on data
silently corrupt warehouses: when two branches both derive from the same
base, "merging" their outputs requires guessing rows' intent. Refusing to
merge is also what keeps branches *cheap* — you never carry merge debt, so
branches can be short-lived and disposable.

## Standalone mode: no git, same verbs

Not in a git repo — an agent session, a scheduler-owned project, a
notebook? Nothing changes except how the change-set is keyed:

```bash
reble run --change-set fix-orders        # explicit id
REBLE_CHANGE_SET=fix-orders reble run    # or from the environment
```

Without git and without an explicit id, the default key is simply `local`.
Every verb then takes the same `--change-set fix-orders` argument. This is
the same surface CI and AI agents use: a stable `--json` envelope with
documented exit codes, and `run`/`diff` emitting versioned event streams.

The related config is [`branching.git_sync`](config.md#branching) — leave
it `true` in git projects, set it to `false` when there is no repo to
read. Reble never runs git; it only reads the branch name when you ask it
to.
