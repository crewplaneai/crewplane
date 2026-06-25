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

After manually updating `pyproject.toml` and `CHANGELOG.md`, run:

```bash
make release-prepare
make release-check
```

Optional tools such as `pipx`, `npm`, and Homebrew may skip locally when absent,
but static release-surface tests should still pass.

To publish PyPI and npm after the checks pass and registry credentials are
configured:

```bash
make release
```

`make release-prepare` synchronizes generated release metadata from
`pyproject.toml`, refreshes `uv.lock`, builds local PyPI and npm artifacts,
writes release manifests, and prepares the Homebrew formula candidate. It fails
if the target version already exists on PyPI or npm.

`make release-check` is state-aware. For unpublished versions it verifies
generated metadata and runs lint, format-check, tests, package checks, and
install smokes. For already completed releases it verifies PyPI, npm, npm
`latest`, Homebrew formula metadata, and the Git tag, then exits successfully
without rerunning pre-publish smokes. It prints a final reminder to verify the
changelog because changelog content is still reviewed manually.

`make release` asks for exact version confirmation, reruns `make release-check`,
publishes PyPI first, publishes npm with the `latest` dist-tag, reconciles npm
`latest`, and creates/pushes the annotated Git tag after both registries verify.
If a release is interrupted after only one registry is updated, fix the issue and
run `make release-pypi` or `make release-npm` to complete the missing side. In
non-TTY npm two-factor flows, use `NPM_PUBLISH_OTP` and `NPM_DIST_TAG_OTP` so
`npm publish` and `npm dist-tag add` each receive a fresh OTP. Homebrew tap
publishing is still a separate maintainer step: copy the prepared formula into
the tap, run audit/test there, and push the tap update.
