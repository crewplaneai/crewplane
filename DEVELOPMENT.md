# Development Guide

This guide is for human contributors. AI-agent project guidance lives in `AGENTS.md`.

## Purpose

Use this document for local setup, repository layout, repeatable development workflows, and architecture references. Public user documentation starts at [docs/index.md](docs/index.md).

## Prerequisites

- Python 3.13+
- `pip` (required)
- `uv` (recommended; required for the repository-automation checks below)

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
make test         # pytest suite with branch coverage
make typecheck    # typed extension contracts + external consumer fixture
make lint         # project-env ruff check src tests scripts
make format       # project-env ruff import fixes + format src tests scripts
make format-check # project-env ruff format --check src tests scripts
make check        # lint + format-check + typing + tests
make help         # list package and release targets
make clean        # remove caches and build artifacts
make uninstall    # uninstall package from current environment
```

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

The Makefile falls back to `python -m ...` for project checks when `uv` is not
installed. The full repository-automation check uses `uvx` for pinned
pre-commit execution.

Current CI policy:

- Default branch: `master`.
- The supported platform matrix is defined in
  [Supported Platforms](#supported-platforms).
- Production publishing is local-only. Follow the
  [Release Workflow](#release-workflow). GitHub Actions does not publish
  production PyPI or npm packages and does not need their credentials.
- Workflow actions are pinned to immutable commits, with version comments kept
  beside each pin for dependency automation and review. Workflow `uv`
  installations are also version-pinned.

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

Use these commands when you need to remove generated files or reset local state:

```bash
# Remove caches and build artifacts
make clean

# Remove generated run outputs only
rm -rf .crewplane/execution-stages .crewplane/execution-results

# Full local reset (config + workflows + outputs)
rm -rf .crewplane

# Uninstall package from current environment
make uninstall
```

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

Production releases have two publication phases: publish the packages and Git
tag locally, then publish the GitHub Release. Use `make help` for target
details.

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

Immediately after `make release` pushes the tag, dispatch
`.github/workflows/release.yml` from `master`. Use the full Git tag, including
the `v` prefix, as the required `tag` input: for package version `0.1.4`, enter
`v0.1.4`, not `0.1.4`. Do this before `master` advances: dispatching from
another ref, backfilling a historical tag, or dispatching after a newer commit
reaches `master` is unsupported.

The workflow verifies the tagged release and publishes the GitHub Release. It
does not publish the production PyPI or npm packages. Prereleases are never
marked as GitHub `Latest`; a stable release is marked `Latest` only when it is
the highest published stable version on PyPI.

### 4. Publish Homebrew separately

Copy the prepared formula into the Homebrew tap, run the tap's audit and test
steps, and push the tap update.

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

## Key Modules

- `src/crewplane/cli/app.py`: Typer app and commands (`init`, `run`, `validate`)
- `src/crewplane/core/config.py`: Pydantic config models and loader
- `src/crewplane/architecture/ports/`: Runtime integration port contracts
- `src/crewplane/architecture/loader.py`: Alias and dotted implementation loader
- `src/crewplane/bootstrap/container.py`: Runtime composition root
- `src/crewplane/core/workflow/models.py`: Workflow model schema
- `src/crewplane/core/workflow/loading.py`: Workflow file loading
- `src/crewplane/core/workflow/markdown/`: Frontmatter and Markdown parser
- `src/crewplane/core/workflow/composition/`: Markdown imports, aliases, params, and input binding
- `src/crewplane/core/workflow/validation/`: Workflow and provider validation
- `src/crewplane/core/preflight/`: Compiled runtime execution-plan previews and bundles
- `src/crewplane/runtime/agent/invoker.py`: Provider command invocation and retry logic
- `src/crewplane/runtime/execution/workflow/__init__.py`: DAG scheduling and node execution
- `src/crewplane/artifacts/manager.py`: Artifact and output manifest management
- `src/crewplane/artifacts/results/`: Consolidated result writing
- `src/crewplane/artifacts/resume/`: Node-boundary resume validation and hydration
- `src/crewplane/artifacts/workspace/`: Workspace artifact validation and descriptors
- `src/crewplane/observability/runtime.py`: Observer lifecycle and snapshot publishing

## Testing Expectations

- New behavior must include tests.
- Bug fixes must include regression tests.
- Keep tests deterministic and filesystem-local.
- Integration implementations must include contract tests under `tests/integration/architecture/` and adapter tests under `tests/integration/adapters/`.
- Public extension contracts must pass `make typecheck`; the package job also
  type-checks the consumer fixture against the built wheel.
- Tests enforce branch coverage.

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

1. Run `crewplane run` with the mock invoker.
2. Confirm node transitions (`pending -> running -> succeeded/failed`) in the CLI or tmux UI.
3. Validate generated artifacts under `.crewplane/execution-stages/` and `.crewplane/execution-results/`, including findings artifacts for findings-enabled nodes, run-root logs in `.crewplane/execution-stages/<workflow>-<run_id>/logs/`, and review-loop status artifacts in `<node>/review-state/review-loop-status.json` when a node uses sequential executor/reviewer review rounds.
4. If using `output_mode: "file"`, verify fixture fallback order, `strict_file_mode` behavior, and optional `<fixture>.mutations.json` sidecars when testing artifact-drift handling, workspace checkout mutations, or prompt sentinel requirements.

## Coding Standards

Follow these project standards:

- Keep modules cohesive and boundaries explicit.
- Use explicit type hints for public APIs and non-trivial logic.
- Keep functions focused and readable.
- Validate at boundaries and fail explicitly.
- Avoid silent failure paths.
- Add deterministic tests for new behavior and regression coverage for bug fixes.

## Architecture References

Crewplane follows a blackboard architecture: agents operate independently and communicate exclusively through structured Markdown artifacts in a shared workspace. That design drives the main engineering constraints in the runtime and artifact system.

- [docs/architecture/modular-orchestration-architecture.md](docs/architecture/modular-orchestration-architecture.md)
- [docs/architecture/adr/0001-ports-adapters-runtime-integrations.md](docs/architecture/adr/0001-ports-adapters-runtime-integrations.md)
- [docs/architecture/index.md](docs/architecture/index.md)

## Adapter Authoring

1. Implement the relevant port contract under `src/crewplane/architecture/ports/`.
2. Register an alias in `src/crewplane/architecture/registry.py` or use a dotted path override in config.
3. Add adapter behavior tests under `tests/integration/adapters/`.
4. Add architecture wiring tests under `tests/integration/architecture/`.
5. Run quality gates before merge:
   - targeted adapter and architecture tests for changed integrations
   - `make lint`
   - `make format-check`
   - `make test`
