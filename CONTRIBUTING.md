# Contributing to Reble

Welcome! We're excited you want to contribute to Reble. This document outlines guidelines and processes for contributing.

## Code of Conduct

We are committed to providing a welcoming and inclusive environment. Please be respectful and constructive in all interactions.

## How to Contribute

### Reporting Bugs

- Check if the bug has already been reported in [Issues](https://github.com/rebleio/reble/issues)
- Create a new issue with:
  - Clear title and description
  - Steps to reproduce
  - Expected vs actual behavior
  - Environment (OS, version, etc.)

### Suggesting Features

- Open a discussion in [Discussions](https://github.com/rebleio/reble/discussions) first
- Or create an issue labeled `enhancement`
- Describe the use case and why it matters

### Submitting Code

1. **Fork the repo** and create a branch for your feature/fix
2. **Write tests** for new functionality
3. **Follow code style** — consistent with existing codebase
4. **Commit with clear messages** — reference issues if applicable
5. **Push and create a Pull Request** with description of changes
6. **Address review feedback** — we'll iterate together

### Development Setup

```bash
git clone https://github.com/yourusername/reble.git
cd reble
# Install dependencies (specifics TBD)
# Run tests
# Start dev server
```

## Project Structure

```
reble/
├── docs/              # Documentation
├── examples/          # Example projects
├── src/               # Source code
│   ├── core/          # Core branching/lineage logic
│   ├── api/           # REST API
│   ├── ui/            # Web UI
│   └── cli/           # Command-line interface
├── tests/             # Test suite
├── docker-compose.yml # Local dev environment
└── README.md
```

## Questions?

- Open a discussion in [GitHub Discussions](https://github.com/rebleio/reble/discussions)
- Check existing docs in `/docs`

Thanks for contributing! 🙌
