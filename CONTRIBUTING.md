# Contributing to Reble

Welcome! Reble is in the **design phase** — the architecture is documented and the
Phase 0 spikes are validated, but the product code is not written yet. Right now the
most valuable contribution is **feedback on the design**.

## How to contribute today

- **Design feedback:** read [docs/architecture.md](docs/architecture.md) and open a
  [Discussion](../../discussions) — especially if you've fought with dev environments,
  Slim CI, or data diffing on a real team.
- **Bug in a spike?** The spikes in `spikes/` are reproducible
  (`python -m venv .venv && .venv/bin/pip install ...` per each spike's RESULTS.md).
  Open an issue with the output.
- **Your workflow:** tell us how you test pipeline changes today. This directly shapes
  what gets built. Open a Discussion.

## Repository layout

```
reble/
├── docs/
│   ├── architecture.md      # Architecture & implementation plan (start here)
│   └── getting-started.md   # Intended v0.1 experience (aspirational for now)
├── spikes/                  # Validated feasibility spikes with reproducible scripts
│   ├── 01-pyiceberg-branches/
│   └── 03-sqlmesh-embedding/
└── src/                     # Product code (coming — see the plan in docs/)
```

## Once code lands

Standard flow: fork → branch → tests for new functionality → PR with a clear
description. Style and tooling details will be documented when `src/` exists.

## Code of conduct

Be respectful and constructive. We're building a tool whose entire premise is making
it safe to try things — the community should feel the same way.

## License and CLA

The Project is Apache 2.0 (see [LICENSE](LICENSE)) — and the CLI stays that way.

Code contributions require a one-time signature of the
[Contributor License Agreement](CLA.md) (a bot will prompt on your first pull
request; it takes about thirty seconds). Being straight about why it exists:
Reble's open-source CLI is permanent, but the project reserves the option of a
commercially licensed *hosted service* someday. The CLA lets that happen without
chasing permissions from every past contributor — your code stays Apache 2.0 for
everyone, forever, regardless. Design feedback, issues, and discussions need no
CLA — only merged code does.
