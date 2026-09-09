# Development Guide

This guide is for human contributors. AI-agent project guidance lives in `AGENTS.md`.

## Purpose

Use this document for local setup, repository layout, repeatable development workflows, and architecture references. Public user documentation starts at [docs/index.md](docs/index.md).

## Prerequisites

- Python 3.13+
- `uv` (recommended)
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

`make setup` installs the project in editable mode with development dependencies.

## Local Workflows

```bash
make test         # full suite with separate statement and branch coverage floors
make coverage-check # check the existing .coverage.json without rerunning tests
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

`make test` enforces the statement and branch coverage floors defined in the
[Makefile](Makefile).

Workspace-enabled Git tests require Git 2.34.1 or newer. CI runs the relevant
source-policy tests against exactly Git 2.34.1 and fails if any selected test
skips.

Nightly CI covers Linux and macOS on Python 3.13 and 3.14, shuffles the full
suite with a reproducible seed, and repeats the focused reviewer-parallelism
regression in fresh processes.

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
- Weekly automation follows the Dependabot Python update. When that PR updates packaging/uv-bootstrap-version.txt, the workflow regenerates all uv bootstrap metadata and commits it to the same branch. Dependabot remains responsible for creating the PR.

Operational notes:

- `pull_request_target` label workflows do not check out or execute pull-request
  code.
- Label definitions synchronize on `master` when `.github/labels.json` changes;
  manual dispatch can intentionally prune labels that are no longer declared.
- Before running `.github/workflows/testpypi.yml`, create the `testpypi` GitHub
  environment under `Settings -> Environments`, then register a free pending
  publisher at <https://test.pypi.org/manage/account/publishing/> with project
  `crewplane`, owner `crewplaneai`, repository `crewplane`, workflow
  `testpypi.yml`, and environment `testpypi`. No repository secret is required.

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
├── tests/
├── docs/                   # Public usage docs plus architecture decision records
├── pyproject.toml
├── Makefile
├── AGENTS.md
└── DEVELOPMENT.md
```

## Version Sources

`pyproject.toml` owns the package distribution version. That version identifies installable releases and should change for every published release.

The authored Python schema version lives in `src/crewplane/version.py`. Generated templates render schema values from that constant. Bump it when supported user-authored config or workflow files change incompatibly. Backward-compatible additions, bug fixes, documentation updates, ordinary package releases, and public-alpha persisted run-artifact hard breaks do not require a schema version bump.

See [ADR 0013](docs/architecture/adr/0013-version-source-of-truth-and-documentation-drift-reduction.md) for the version source-of-truth decision.

| Version | Governs | Bump When |
| --- | --- | --- |
| `pyproject.toml` `project.version` | installable package release | every published release |
| `SCHEMA_VERSION` | current config files, workflow files, and preflight execution-plan artifacts | supported user-authored schema changes incompatibly |

During the public-alpha `0.x` period, support the current schema only. Persisted run artifacts are disposable audit outputs, not migration targets; stale preflight plans may be rejected by explicit shape validation even when they carry the current `SCHEMA_VERSION`.

## Release Workflow

Production releases publish PyPI, npm, and the Git tag locally. The release
Action then publishes the GitHub Release and opens a Homebrew pull request for
the newest stable release. Publishing the tested bottles remains a manual
`brew pr-pull` step.

### 1. Prepare and validate

Update the version in `pyproject.toml` and add the matching section to
`CHANGELOG.md`. Review the changelog content manually, then run:

```bash
make release-prepare
make release-check
```

Both commands must pass before publication. Preparation stops if the target
version already exists on PyPI or npm. Some install checks may be skipped when
optional local tools such as `pipx`, npm, or Homebrew are unavailable; review
the skip messages before continuing.

### 2. Publish packages and the Git tag

Configure the PyPI and npm credentials, then run:

```bash
make release
```

Confirm the exact version when prompted. A successful run publishes PyPI,
publishes npm with the `latest` dist-tag, and pushes the annotated Git tag.

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

If only part of PyPI or one registry was published, fix the reported problem
and rerun the corresponding target:

```bash
make release-pypi
make release-npm
```

These targets verify anything already published and complete only the missing
work. Use `make release-npm` when the npm package exists but its `latest`
dist-tag is stale. Once both registries are complete, rerun `make release` to
finish the Git tag.

### TestPyPI

Use `.github/workflows/testpypi.yml` for TestPyPI Trusted Publishing. It may be
dispatched from any selected ref and stops if that package version already
exists on TestPyPI.

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
- Integration implementations must include contract tests under `tests/integration/architecture/` and adapter tests under `tests/integration/adapters/`.
- Production code and typing fixtures must pass strict mypy via `make typecheck`;
  CI also validates the built wheel's public typing.
- Coverage requirements are defined in the [test suite contract](#test-suite-contract).

Examples of focused test runs:

```bash
uv run --extra dev python -m pytest -q tests/integration/cli/test_workflow_discovery_and_init.py
uv run --extra dev python -m pytest -q tests/unit/core/workflow_composition tests/unit/core/workflow_validation
uv run --extra dev python -m pytest -q tests/integration/adapters/mock_invoker tests/integration/architecture/test_container.py
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

Crewplane follows a blackboard architecture: providers coordinate through durable
artifacts on disk rather than shared in-memory state. Artifacts include Markdown
outputs, JSON state, and Git bundles for workspace handoffs.

- [docs/architecture/modular-orchestration-architecture.md](docs/architecture/modular-orchestration-architecture.md)
- [docs/architecture/adr/0001-ports-adapters-runtime-integrations.md](docs/architecture/adr/0001-ports-adapters-runtime-integrations.md)
- [docs/architecture/index.md](docs/architecture/index.md)

## Adapter Authoring

1. Implement the relevant port contract under `src/crewplane/architecture/ports/`.
2. Register an alias in `src/crewplane/architecture/registry.py` or use a dotted path override in config.
3. Add adapter behavior tests under `tests/integration/adapters/`.
4. Add architecture wiring tests under `tests/integration/architecture/`.
5. Run focused adapter and architecture tests, then `make check`.
