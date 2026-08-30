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

## License

Contributions are accepted under Apache 2.0 (see [LICENSE](LICENSE)).
