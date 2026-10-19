# Development Guide

This guide is for human contributors. AI-agent project guidance lives in `AGENTS.md`.

## Purpose

Use this document for local setup, repository layout, repeatable development workflows, and architecture references. Public user documentation starts at [docs/index.md](docs/index.md).

## Prerequisites

- Python 3.13+
- `uv` (required for maintained development and test commands)
- `pip` (required)
- Node.js 18+ and `npm` when working on the npm wrapper

## Supported Platforms

Crewplane supports Python 3.13 and newer on Linux, macOS, and WSL when the
configured provider CLIs are available on that platform. Native Windows is not
supported; use WSL on Windows hosts.

Pull-request CI runs on Linux for Python 3.13 and 3.14. Nightly CI runs on
Linux and macOS for Python 3.13 and 3.14.

The tmux live dashboard requires `tmux` and is intended for Unix-like
environments. WSL supports the same tmux live mode as Linux.

## Setup

```bash
cd crewplane
make setup
```

`make setup` installs the editable project and development dependencies from
`uv.lock`. For shuffled profiles (`pr-313` and `nightly-shuffle`), run
`make setup TEST_PROFILE=pr-313` to include the `stress` extra.

## Local Workflows

```bash
make test         # full suite with separate statement and branch coverage floors
make coverage-check COVERAGE_REPORT='test-results/<invocation>/coverage.json'
make typecheck    # strict type checking for package and fixtures
make lint         # project-env ruff check src tests scripts
make format       # modifies files: ruff import fixes + format src tests scripts
make format-check # project-env ruff format --check src tests scripts
make check        # lint + format-check + typing + uv pin check + tests
make uv-bootstrap-check  # verify all pinned uv versions and checksums agree
make uv-bootstrap-update # update all uv pins and checksums to the latest release
make help         # list package and release targets
make clean        # remove caches and build artifacts
make uninstall    # uninstall package from current environment
```

For code changes, run focused tests during development and `make check` before
handoff. Documentation-only changes require checks of affected links, paths,
commands, and examples. Run the additional automation or packaging checks below
when the changed files affect those areas. Use `make format` when formatting
needs correction; it modifies files and is separate from the validation gate.

### Test suite contract

Local, CI, nightly, and release validation share test execution and package
acceptance checks; use the profiles and package commands below to reproduce the
relevant checks locally.

By default, `make test` and `make check` run the full suite with minimum coverage
of 96% statements and 90% branches. Use `--coverage off` for
[focused test runs](#testing-expectations).

Git fixtures require Git 2.34.1 or newer. Dependent tests skip locally if it is
unavailable; CI profiles fail. Set `CREWPLANE_REQUIRED_GIT=1` to require it locally.

Select a CI profile with `make test TEST_PROFILE=pr-313`, or force serial execution
with `make test TEST_ARGS='--workers 0'`. Use `make test TEST_ARGS=--help` for all
profiles and options.

Each run prints its `test-results/<profile>-<id>/` directory and a reproduction
command, also saved in `metadata.json`. Check `pytest.log` for failures and reproduce
with the same Python version, locked dependencies, and platform. To recheck coverage,
use the `make coverage-check` command above with that run's directory name.

For packaging changes, build and validate the development artifacts:

```bash
make package-build
uv run --locked --extra dev twine check dist/*
make wheel-typecheck
make install-smoke
```

Use `make package-check` after [release preparation](#1-prepare-and-validate),
when the Homebrew formula checksum has been regenerated for the source archive.

## Repository Automation

GitHub Actions, issue templates, label automation, and community files are
tailored for the public `crewplane` repository. Use the same entry points
locally and in CI:

```bash
make setup
make check
make actionlint
uvx pre-commit==4.6.0 run --all-files --show-diff-on-failure
```

The full repository-automation check uses `uvx` for pinned pre-commit execution.

Current CI policy:

- Default branch: `master`.
- The supported platform matrix is defined in
  [Supported Platforms](#supported-platforms).
- Production PyPI and npm publishing is local-only. Follow the
  [Release Workflow](#release-workflow). After `make release` publishes the
  packages and Git tag, the source repository's release Action publishes the
  GitHub Release and opens the Homebrew pull request.
- Workflow actions and `uv` are version-pinned. `packaging/uv-bootstrap.json`
  is the source of truth for the `uv` version and installer checksums. The
  updater generates `packaging/uv-bootstrap-version.txt` from this manifest for
  workflows; do not edit the generated file directly. This keeps scheduled
  updates from rewriting workflow definitions.
- uv bootstrap metadata is synchronized automatically after Dependabot PR checks
  complete, including failed checks. Weekly and manual runs remain available
  for recovery. Dependabot creates the PR.

Operational notes:

- `pull_request_target` label workflows do not check out or execute pull-request
  code.
- Label definitions synchronize on `master` when `.github/labels.json` changes;
  manual dispatch can intentionally prune labels that are no longer declared.
- Before running `.github/workflows/testpypi.yml`, create the `testpypi` GitHub
  environment under `Settings -> Environments`, then register a free pending
  publisher at <https://test.pypi.org/manage/account/publishing/> with project
  `crewplane`, owner `crewplaneai`, repository `crewplane`, workflow
  `testpypi.yml`, and environment `testpypi`. Register the same publisher for
  `crewplane-ollama`, which is uploaded by the same workflow. No repository
  secret is required.

## Cleanup and Deletion

For runs that created managed workspaces, clean up their caches before deleting
run records or `.crewplane`. Preview the intended scope from the originating
Git checkout, with its configuration and a valid HEAD commit:

```bash
uv run --extra dev crewplane cleanup workspaces --dry-run
```

Follow the [cleanup guide](docs/guides/cleanup.md) to remove the selected caches
and inspect the result. Keep configuration and run records for any entries
cleanup skips; resolve those entries before a complete reset. Workspace cleanup
uses these records to verify ownership and eligibility, and caches can live
outside the project directory.

After workspace cleanup, archive or delete the corresponding run records under
`.crewplane/execution-stages/` and `.crewplane/execution-results/`. A full reset
also deletes the authored config and workflows under `.crewplane`; preserve any
that you intend to reuse before removing that directory.

Development caches and build artifacts have a separate cleanup command:

```bash
make clean
```

Use `make uninstall` to uninstall the package from the development environment.

## Project Structure

```text
crewplane/
├── src/
│   └── crewplane/
│       ├── cli/            # CLI command surface, run helpers, cleanup, templates
│       ├── core/           # Config/workflow schemas, parsing, composition, preflight
│       ├── architecture/   # Stable integration contracts, loader, registry
│       ├── adapters/       # Built-in integration implementations
│       ├── bootstrap/      # Composition root for runtime components
│       ├── runtime/        # Agent invocation and workflow execution
│       ├── artifacts/      # Output directories, manifests, results, resume, workspace state
│       ├── observability/  # Runtime event model, layout/rendering, tmux dashboard
│       └── example_templates/
├── plugins/                # Independently packaged extensions
├── tests/
├── docs/                   # User and architecture documentation
├── pyproject.toml
├── Makefile
├── AGENTS.md
└── DEVELOPMENT.md
```

## Version Sources

| Source | Governs | When to change |
| --- | --- | --- |
| `project.version` in `pyproject.toml` | Installable package version | Every published release |
| `SCHEMA_VERSION` in `src/crewplane/version.py` | Config, workflow, and preflight plan schemas | Incompatible changes to supported user-authored config or workflows |

Generated templates use `SCHEMA_VERSION`. Ordinary releases and backward-compatible
changes do not require a schema bump. During public alpha (`0.x`), support only the
current schema; persisted run artifacts are disposable and need no migrations.

## Release Workflow

Production releases publish PyPI, npm, and the Git tag locally. The release
Action then publishes the GitHub Release and opens a Homebrew pull request for
the newest stable release. Publishing the tested bottles remains a manual
`brew pr-pull` step.

### 1. Prepare and validate

Update the version in `pyproject.toml` and add its section to `CHANGELOG.md`
with any notable user-facing changes. When the Ollama plugin changes, also bump
its version in `plugins/crewplane-ollama/pyproject.toml` and ensure the core
dependency accepts it. `make setup` installs both packages from this checkout;
run `uv lock` after changing their requirements or versions.

Review the changelog, then prepare the
release with npm installed:

```bash
make release-prepare
```

Review, commit, and push the version change and generated metadata to `master`.
Then validate from the clean, synchronized checkout:

```bash
make release-check
```

Both commands must pass before publication. Preparation stops if the target
version already exists on PyPI or npm. Some install checks may be skipped when
optional local tools such as `pipx` or Homebrew are unavailable; review the skip
messages before continuing.

### 2. Publish packages and the Git tag

After `make release-check` passes, configure the PyPI and npm credentials, then
run:

```bash
make release
```

Confirm the exact version when prompted. A successful run publishes the Ollama
plugin to PyPI before Crewplane, publishes npm with the `latest` dist-tag, and
pushes the annotated Git tag. PyPI credentials must permit uploads to both
projects. Existing plugin artifacts are verified and reused; changed artifacts
require a new plugin version.

For a non-interactive npm release that requires two-factor authentication, set
`NPM_PUBLISH_OTP` or `NPM_OTP`. A separate npm `latest` recovery uses
`NPM_DIST_TAG_OTP` or `NPM_OTP`.

### 3. Publish the GitHub Release

After `make release` pushes the tag, run the source repository's `release`
GitHub Action from `master`. Enter the full Git tag, including the `v` prefix:
for package version `0.1.4`, enter `v0.1.4`.
Dispatch it before `master` advances; the tag must point to the currently
selected `master` commit.

The Action publishes the GitHub Release. For the newest stable release, it also
automatically opens a pull request in `crewplaneai/homebrew-crewplane`.

### 4. Publish the tested Homebrew pull request

1. Wait for both the macOS and Linux `brew test-bot` checks to pass and upload
   their bottles.
2. Do **not** click the pull request's normal Merge button.
3. In `homebrew-crewplane`, run the `brew pr-pull` Action with:

   - The pull request number.
   - Preferably the pull request's current head SHA, which prevents publishing
     a revision that was not tested.

The `brew pr-pull` Action collects the tested bottles, updates the formula's
bottle metadata, and pushes the completed release to `main`.

### Recover an interrupted release

Recover from the commit validated by `make release-check`, using the same
prepared artifacts. Fix the reported problem, then rerun the affected target:

```bash
make release-pypi
make release-npm
```

These targets verify existing uploads and complete missing publication steps.
Use `make release-npm` to repair a stale `latest` tag; it refuses to replace a
newer release. Once both registries are complete, rerun `make release` to finish
the Git tag. Do not run `make clean` or `make release-prepare` during recovery.

### TestPyPI

Use `.github/workflows/testpypi.yml` for TestPyPI Trusted Publishing. It publishes
Crewplane and its default plugins from the selected ref. Each distribution needs
a version that has not been uploaded to TestPyPI; the workflow stops on an
existing version.

## Module and Test Map

Use this map to locate implementation entry points and affected tests. Change-specific
constraints live in [AGENTS.md](AGENTS.md#change-guidance).

### CLI surface

- Main entrypoint: `src/crewplane/cli/app.py`
- Supporting run flow: `src/crewplane/cli/run/` plus the `src/crewplane/cli/workflow_runner.py` facade
- Cleanup command surface: `src/crewplane/cli/cleanup.py`
- Path resolution and scaffold helpers: `src/crewplane/cli/paths.py`, `src/crewplane/cli/templates.py`
- Expected tests: `tests/integration/cli/`, plus any affected unit tests under `tests/unit/`

### Workflow schema, parsing, and composition

- Core files: `src/crewplane/core/workflow/models.py`, `src/crewplane/core/workflow/markdown/`, `src/crewplane/core/workflow/loading.py`, `src/crewplane/core/workflow/composition/`, `src/crewplane/core/workflow/validation/`, `src/crewplane/core/preflight/`
- Expected tests: `tests/unit/core/workflow_loading/`, `tests/unit/core/workflow_composition/`, `tests/unit/core/workflow_validation/`, `tests/unit/core/preflight/`, and relevant `tests/integration/cli/` coverage

### Config and provider invocation

- Core config: `src/crewplane/core/config.py`, `src/crewplane/core/workspace/settings.py`, `src/crewplane/core/token_budget.py`
- Runtime invoker path: `src/crewplane/runtime/agent/`
- Built-in invokers: `src/crewplane/adapters/invokers/`
- Expected tests: `tests/unit/core/test_config.py`, `tests/integration/runtime/agent/`, `tests/integration/adapters/test_invoker_cli.py`, and `tests/integration/adapters/mock_invoker/`

### Runtime execution

- Workflow scheduler: `src/crewplane/runtime/execution/workflow/__init__.py`
- Stage execution: `src/crewplane/runtime/execution/parallel.py`, `src/crewplane/runtime/execution/sequential.py`, `src/crewplane/runtime/execution/consensus.py`
- Expected tests: `tests/integration/runtime/execution/`, `tests/integration/cli/test_workflow_runner.py`, and affected `tests/unit/runtime/` coverage

### Adapters and architecture boundaries

- Port contracts: `src/crewplane/architecture/ports/`
- Alias registry: `src/crewplane/architecture/registry.py`
- Loader: `src/crewplane/architecture/loader.py`
- Composition root: `src/crewplane/bootstrap/container.py`
- Expected tests: `tests/integration/architecture/`, relevant `tests/integration/adapters/`

### Plugins

- Public CLI capability API: `src/crewplane/adapters/invokers/cli_invoker/__init__.py`
- Registration and package identity: `src/crewplane/adapters/invokers/cli_invoker/provider_registrations.py`
- Plugin packages: `plugins/`, including `plugins/crewplane-ollama/`
- Expected tests: `tests/unit/adapters/invokers/cli_invoker/`, `tests/integration/adapters/ollama/`, `tests/integration/adapters/test_cli_provider_lifecycle.py`, and CLI provider tests under `tests/integration/architecture/`

### Artifacts, manifests, and templates

- Core files: `src/crewplane/artifacts/manager.py`, `src/crewplane/artifacts/directory_manager.py`, `src/crewplane/artifacts/generated_files/`, `src/crewplane/artifacts/locks/`, `src/crewplane/artifacts/results/`, `src/crewplane/artifacts/resume/`, `src/crewplane/artifacts/workspace/`, and `src/crewplane/core/preflight/`
- Built-in implementation: `src/crewplane/adapters/artifacts/filesystem.py`
- Expected tests: `tests/unit/artifacts/`, `tests/integration/adapters/test_artifacts_filesystem.py`, and affected `tests/integration/cli/` coverage

### Observability and tmux UI

- Core files: `src/crewplane/observability/`, `src/crewplane/adapters/ui/`
- Expected tests: `tests/integration/observability/`, `tests/unit/observability/`, `tests/integration/adapters/test_ui_tmux.py`, `tests/integration/adapters/test_ui_null.py`

## Testing Expectations

- New behavior must include tests.
- Bug fixes must include regression tests.
- Keep tests deterministic and filesystem-local.
- Mark individual tests with `@pytest.mark.race` when they exercise a specific
  concurrency or cancellation interleaving and assert the affected invariant.
  Use controlled synchronization and bounded cleanup. Run them with
  `make test TEST_PROFILE=race`; nightly CI repeats this selection five times on
  Linux and native Windows with Python 3.13.
- Integration implementations must include contract tests under `tests/integration/architecture/` and adapter tests under `tests/integration/adapters/`.
- Production code and typing fixtures must pass strict mypy via `make typecheck`;
  CI also validates the built wheel's public typing.
- Coverage requirements are defined in the [test suite contract](#test-suite-contract).

Examples of focused test runs:

```bash
uv run --locked --extra dev python scripts/run_tests.py --profile local --coverage off tests/integration/cli/test_workflow_discovery_and_init.py
uv run --locked --extra dev python scripts/run_tests.py --profile local --coverage off tests/unit/core/workflow_composition tests/unit/core/workflow_validation
uv run --locked --extra dev python scripts/run_tests.py --profile local --coverage off tests/integration/adapters/mock_invoker tests/integration/architecture/test_container.py
```

## Mock Invoker Local Validation

Use the `mock` invoker integration for deterministic orchestration and UI checks without provider CLI calls:

```yaml
settings:
  integrations:
    invoker:
      implementation: "mock"
      options:
        delay_seconds: 0.25
        observation_delay_seconds: 5
        output_mode: "lorem"
        seed: 42
```

`output_mode: "lorem"` also auto-emits a deterministic findings block for non-reviewer nodes that declare `findings: true`, so findings-based workflows can be exercised locally without hand-written fixtures. `echo` mode is exact for non-reviewer invocations, and fixture-backed `file` output is always exact; those authored outputs must include the findings block themselves when needed. Reviewer invocations in `echo`, `lorem`, and missing-fixture fallback paths emit a deterministic no-findings review contract.

`observation_delay_seconds` keeps mock runs visibly active in the live dashboard for a few seconds by default; set it to `0` when a test or local check should complete immediately.

Manual validation flow:

1. For a new validation project, run `<checkout>/.venv/bin/crewplane init` in that project's directory, replacing `<checkout>` with the absolute path to this source checkout. Use that same absolute CLI path for the following steps in another project. `init` preserves existing files, so verify that the selected config uses the mock invoker before running a workflow.
2. Run `uv run --extra dev crewplane validate`, then `uv run --extra dev crewplane run --dry-run`.
3. Run `uv run --extra dev crewplane run --no-live` with the mock invoker. Omit `--no-live` when checking the live UI.
4. Confirm node transitions (`pending -> running -> succeeded/failed`) in the CLI or tmux UI.
5. Validate generated artifacts under `.crewplane/execution-stages/` and `.crewplane/execution-results/`, including findings artifacts for findings-enabled nodes, run-root logs in `.crewplane/execution-stages/<workflow>-<run_id>/logs/`, and review-loop status artifacts in `<node>/review-state/review-loop-status.json` when a node uses sequential executor/reviewer review rounds. When affected, confirm manifest dedupe behavior against the intended `workflow_signature` rules.
6. If using `output_mode: "file"`, verify fixture fallback order, `strict_file_mode` behavior, and optional `<fixture>.mutations.json` sidecars when testing artifact-drift handling, workspace checkout mutations, or prompt sentinel requirements.

## Coding Standards

Follow the canonical [coding standards](AGENTS.md#coding-standards) and
[change guidance](AGENTS.md#change-guidance) in `AGENTS.md`.

## Maintaining Agent Guidance

When changing agent behavior rules, verify the change with a representative
task in a fresh session. Confirm that the intended instructions load and the
expected checks run. Revise rules in response to observed failures, and remove
obsolete or duplicated guidance. Record any behavior that could not be verified
in the handoff.

## Architecture References

See the [architecture guide](docs/architecture/index.md) for module boundaries and
runtime contracts.

## Adapter Authoring

1. Implement the relevant port contract under `src/crewplane/architecture/ports/`.
2. Register an alias in `src/crewplane/architecture/registry.py` or use a dotted path override in config.
3. Add adapter behavior tests under `tests/integration/adapters/`.
4. Add architecture wiring tests under `tests/integration/architecture/`.
5. Run focused adapter and architecture tests, then `make check`.

### CLI capability packages

Additional CLI providers use the public `CliProviderCapability` contract in
`crewplane.adapters.invokers.cli_invoker`. Keep independently buildable packages
under `plugins/`, register their exports through
`settings.integrations.invoker.options.providers`, and follow the adapter test
requirements above.

`make check` includes plugin sources. With package-index access, run
`make ollama-package-check` to verify the core and Ollama distributions in an
isolated environment. For changes to the Ollama integration, also verify
readiness, output handoff, failure handling, and cancellation with a live model
on Linux and macOS.

`make package-build` builds Crewplane and the default plugins declared under
`tool.crewplane.release.plugins` into `dist/`. The core source archive includes
their sources so Homebrew can install them in its managed environment. Install
checks use these local wheels; release publication verifies and uploads the
plugin distributions before the core distribution.
