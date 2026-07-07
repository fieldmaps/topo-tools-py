# Contributing

Thanks for contributing! This guide covers how to get set up, how we work,
and what a finished pull request looks like.

## Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
git clone https://github.com/fieldmaps/topo-tools-py.git
cd topo-tools-py
uv sync
uv run pre-commit install
```

## Running the tool locally

```bash
uv run topo-tools extend --input-file=... --output-file=...
# equivalently: uv run python -m topo_tools extend ...
```

## Tests

```bash
uv run pytest
```

Tests use small synthetic geometry generated in-fixture (see
`tests/test_extend.py`) rather than committed binary fixtures — prefer that
pattern for new tests unless a bug genuinely requires a real-world file to
reproduce.

## Code quality

```bash
uv run ruff format
uv run ruff check
```

`ruff` runs with `select = ["ALL"]` (see `pyproject.toml`); `pre-commit`
runs the same checks locally that CI runs, so a check should never pass
locally and fail in CI.

## Architecture

Read `CLAUDE.md` before making structural changes — it documents the
three-layer split (`core/` → `api/` → `cli/`) and the rule that `core/` and
`api/` must never import `click`. New tools should follow the same
`core/api/cli` layering as `extend`.

## What a finished PR looks like

- [ ] Tests added/updated for the change
- [ ] `uv run pytest` passes
- [ ] `uv run ruff format && uv run ruff check` clean
- [ ] Docs updated (README, `CLAUDE.md`, or `docs/*.md`) if user-facing
      behavior changed
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` for user-visible
      changes

## Reporting bugs / requesting features

Use the issue templates. A failing test that reproduces the bug is the most
useful bug report you can give us.

## Security issues

Do not open a public issue for a security vulnerability — see
[`SECURITY.md`](SECURITY.md).
