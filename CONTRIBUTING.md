# Contributing to Reble

Thanks for helping. The short version:

- **Read the contract first.** [SPEC.md](SPEC.md) is normative — invariants,
  command surface, envelope, exit codes. [DECISIONS.md](DECISIONS.md) records
  every behavior decision; a PR that changes behavior must say which decision
  it amends. A PR that violates a load-bearing invariant needs justification
  in its description.
- **One core, two adapters.** All orchestration lives in `reble/core.py`;
  the CLI and MCP server render what verbs return. New capability goes in
  the core, never as frontend-private logic.
- **Tests are the acceptance bar.** `pytest tests/ -q` and
  `ruff check reble/ tests/` must pass. The CLI test suite doubling as the
  core's contract test is deliberate — don't weaken it to make a change pass.
- **Keep envelopes additive.** `--json` output and event records change
  additively within a minor version only.
- **No secrets in reble.yml** — `${ENV_VAR}` interpolation only.

Setup:

```bash
git clone https://github.com/satya1395/reble
cd reble
uv venv --python 3.13 && uv pip install -e ".[dev,mcp]"
pytest tests/ -q
```

Try the runnable example in `examples/orders-lakehouse/` to feel the loop.
