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
`pyproject.toml`, refreshes `uv.lock`, builds local PyPI and npm artifacts plus
the offline runtime wheelhouse, writes release manifests, and prepares the
Homebrew formula candidate. It fails if the target version already exists on
PyPI or npm.

The exact Hatchling build-system pin keeps the manual release and its immediately
following GitHub Release rebuild on the same backend version. The manually
dispatched `release-artifacts` command rebuilds the registry artifacts needed for
verification but skips the offline wheelhouse, which stays local to release
preparation and explicit wheelhouse checks.

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
that registry-recovery path, `make release` and the publish targets allow local
worktree and branch state to differ while still verifying generated metadata,
release-manifest artifacts, registry artifacts, and tag conflicts. In
non-TTY npm two-factor flows, use `NPM_PUBLISH_OTP` and `NPM_DIST_TAG_OTP` so
`npm publish` and `npm dist-tag add` each receive a fresh OTP. Homebrew tap
publishing is still a separate maintainer step: copy the prepared formula into
the tap, run audit/test there, and push the tap update.

Immediately after the manual `make release` flow creates and pushes the tag,
dispatch `.github/workflows/release.yml` from `master` and provide that tag as
the required `tag` input, before `master` advances. The workflow rejects any
other dispatch ref. At the start of verification, it checks out the requested
tag, resolves its commit, and requires it to match the master commit that
dispatched the workflow exactly. Historical-tag backfills and new dispatches
after `master` advances are unsupported. It rebuilds from that tagged source
with `scripts/release.py release-artifacts`, verifies PyPI, npm, Homebrew,
manifest, local artifact, and tag state with
`scripts/release.py github-release-plan`, and transfers the verified artifacts
and manifest to the write-authorized job. The publishing job checks out the
exact verified commit, verifies fresh external state before mutating GitHub,
repeats the plan, and supplies the verified predecessor tag explicitly when
GitHub generates release notes. Every plan evaluation re-fetches
`origin/master` and requires the release commit to remain reachable from it. The
publisher then reloads and re-verifies draft assets immediately before
publication. The GitHub Release contains `dist/*`; the npm tarball is an
integrity input, not a GitHub Release asset. It verifies exact asset names,
sizes, and GitHub SHA-256 digests before and after publication, rejects
unexpected draft assets, and never mutates an already-published mismatch. The
release title must match the tag, and generated notes carry a hidden, versioned
automation marker; existing drafts without both are rejected before asset
mutation.
Prereleases can never be GitHub `Latest`. A stable release is `Latest` only when
it is the highest published stable version on PyPI. A repository-wide
non-dropping queue serializes publication workflows around those checks and
mutations. PyPI and npm remain the production publishing sources of truth; the
workflow does not publish production packages.
`.github/workflows/testpypi.yml` is the separate TestPyPI Trusted Publishing
workflow. Maintainers may intentionally dispatch it from any selected ref; it is
not restricted to `master`. It fails if the package version already exists on
TestPyPI.
