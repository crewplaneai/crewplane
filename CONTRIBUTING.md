# Contributing

Thanks for contributing to `crewplane`.

See [GOVERNANCE.md](GOVERNANCE.md) for project roles and decision making, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for participation expectations.

## Local Setup

```bash
make setup
make check
```

Use Python 3.13 or newer. The Makefile falls back to `python -m ...` for project
checks when possible; install `uv` to run the full automation checks below.

Crewplane supports Python 3.13+ on Linux, macOS, and WSL when the configured
provider CLIs are available. Native Windows is not supported; use WSL on Windows
hosts. Pull-request CI runs on Linux for Python 3.13 and 3.14; nightly CI also
covers macOS. See
[Supported Platforms](DEVELOPMENT.md#supported-platforms) for the current
platform policy and tmux live UI notes.

## Pull Requests

- Use a Conventional Commit-style PR title, such as
  `fix(runtime): handle failed provider output`.
- Keep PRs focused enough for a maintainer to review in one pass.
- Run `make check` before opening a PR when the change touches code, workflows,
  config, or docs.
- For GitHub Actions or community-file changes, also run:

```bash
make actionlint
uvx pre-commit==4.6.0 run --all-files --show-diff-on-failure
```

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
