# Contributing

Thanks for contributing to `crewplane`.

## Local Setup

```bash
make setup
make check
```

Use Python 3.13 or newer. `uv` is optional but recommended; the Makefile falls
back to `python -m ...` commands when possible.

## Development Rules

- Read our [architecture guidance](docs/architecture/modular-orchestration-architecture.md) first.
- Keep runtime orchestration behavior artifact-backed and auditable under
  `.crewplane/`.
- Keep provider-specific command handling inside adapter or invoker boundaries.
- Add deterministic pytest coverage for behavior changes.
- Keep test-only helpers under `tests/`.
- Update docs and example templates when CLI flags, config keys, workflow
  syntax, or install behavior changes.
- For node or DAG rendering issues, reference
  [the UI compact dashboard](docs/architecture/ui_compact_dashboard.md) for the
  intended graph behavior and add or update the relevant layout fixture under
  [tests/unit/observability/fixtures/](tests/unit/observability/fixtures/).

## Release Surface Checks

Before package-name reservation or alpha publishing, run:

```bash
make release-check
```

Optional tools such as `pipx`, `npm`, and Homebrew may skip locally when absent,
but static release-surface tests should still pass.

To publish PyPI and npm after the checks pass and registry credentials are
configured:

```bash
make release
```

Release targets read the version from `pyproject.toml`, require packaging
metadata to match, and fail if the exact version already exists on PyPI or npm.
`make release` asks for version confirmation before any checks or uploads, then
publishes PyPI first and npm with `NPM_TAG=alpha` by default. Homebrew tap
publishing is still a separate maintainer step.
