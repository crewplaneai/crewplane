# Development Guide

This guide is for human contributors. AI-agent project guidance lives in `AGENTS.md`.

## Purpose

Use this document for local setup, repository layout, repeatable development workflows, and architecture references.

## Prerequisites

- Python 3.13+
- `pip` (required)
- `uv` (optional, used automatically by `Makefile` if available)

## Setup

```bash
cd orchestrator_cli
make setup
```

`make setup` installs the project in editable mode with development dependencies.

## Local Workflows

```bash
make test         # project-env pytest -q
make lint         # project-env ruff check src tests
make format       # project-env ruff format src tests
make format-check # project-env ruff format --check src tests
make check        # lint + format-check + tests
make clean        # remove caches and build artifacts
```

## Cleanup and Deletion

Use these commands when you need to remove generated files or reset local state:

```bash
# Remove caches and build artifacts
make clean

# Remove generated run outputs only
rm -rf .orchestrator/execution-stages .orchestrator/execution-results

# Full local reset (config + workflows + outputs)
rm -rf .orchestrator

# Uninstall package from current environment
python -m pip uninstall orchestrator-cli
```

## Project Structure

```text
orchestrator_cli/
├── src/
│   └── orchestrator_cli/
│       ├── cli/            # CLI command surface and orchestration helpers
│       ├── core/           # Config/workflow schemas, parsing, validation, DAG graph
│       ├── architecture/   # Stable integration contracts, loader, registry
│       ├── adapters/       # Built-in integration implementations
│       ├── bootstrap/      # Composition root for runtime components
│       ├── runtime/        # Agent invocation and workflow execution
│       ├── artifacts/      # Output directories, manifests, template resolution
│       ├── observability/  # Runtime event model, layout/rendering, tmux dashboard
│       └── example_templates/
├── tests/
├── docs/
│   └── architecture/
├── pyproject.toml
├── Makefile
├── AGENTS.md
└── DEVELOPMENT.md
```

## Version Sources

`pyproject.toml` owns the package distribution version. That version identifies installable releases and should change for every published release.

The authored Python schema version lives in `src/orchestrator_cli/version.py`. Generated templates render schema values from that constant. Bump it when supported user-authored config or workflow files change incompatibly. Backward-compatible additions, bug fixes, documentation updates, ordinary package releases, and public-alpha persisted run-artifact hard breaks do not require a schema version bump.

See [ADR 0013](docs/architecture/adr/0013-version-source-of-truth-and-documentation-drift-reduction.md) for the version source-of-truth decision.

| Version | Governs | Bump When |
| --- | --- | --- |
| `pyproject.toml` `project.version` | installable package release | every published release |
| `SCHEMA_VERSION` | current config files, workflow files, and preflight execution-plan artifacts | supported user-authored schema changes incompatibly |

During the public-alpha `0.x` period, support the current schema unless a migration path is deliberately added and covered by tests. Persisted run artifacts are disposable audit outputs, not migration targets; stale preflight plans may be rejected by explicit shape validation even when they carry the current `SCHEMA_VERSION`.

## Key Modules

- `src/orchestrator_cli/cli/app.py`: Typer app and commands (`init`, `run`, `validate`)
- `src/orchestrator_cli/core/config.py`: Pydantic config models and loader
- `src/orchestrator_cli/architecture/ports/`: Runtime integration port contracts
- `src/orchestrator_cli/architecture/loader.py`: Alias and dotted implementation loader
- `src/orchestrator_cli/bootstrap/container.py`: Runtime composition root
- `src/orchestrator_cli/core/workflow_models.py`: Workflow model schema
- `src/orchestrator_cli/core/workflow_loader.py`: Workflow file loading
- `src/orchestrator_cli/core/workflow_markdown/__init__.py`: Frontmatter and Markdown parser
- `src/orchestrator_cli/core/workflow_validation.py`: Workflow and provider validation
- `src/orchestrator_cli/runtime/agent/invoker.py`: Provider command invocation and retry logic
- `src/orchestrator_cli/runtime/execution/workflow.py`: DAG scheduling and node execution
- `src/orchestrator_cli/artifacts/manager.py`: Artifact and output manifest management
- `src/orchestrator_cli/observability/runtime.py`: Observer lifecycle and snapshot publishing

## Testing Expectations

- New behavior must include tests.
- Bug fixes must include regression tests.
- Keep tests deterministic and filesystem-local.
- Integration implementations must include contract tests under `tests/architecture/` and `tests/adapters/`.

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

1. Run `orchestrator run` with the mock invoker.
2. Confirm node transitions (`pending -> running -> succeeded/failed`) in the CLI or tmux UI.
3. Validate generated artifacts under `.orchestrator/execution-stages/` and `.orchestrator/execution-results/`, including findings artifacts for findings-enabled nodes, run-root logs in `.orchestrator/execution-stages/<workflow>-<run_id>/logs/`, and review-loop status artifacts in `<node>/review-state/review-loop-status.json` when a node uses sequential executor/reviewer review rounds.
4. If using `output_mode: "file"`, verify fixture fallback order, `strict_file_mode` behavior, and optional `<fixture>.mutations.json` sidecars when testing artifact-drift handling.

## Coding Standards

Follow these project standards:

- Keep modules cohesive and boundaries explicit.
- Use explicit type hints for public APIs and non-trivial logic.
- Keep functions focused and readable.
- Validate at boundaries and fail explicitly.
- Avoid silent failure paths.
- Add deterministic tests for new behavior and regression coverage for bug fixes.

## Architecture References

The orchestrator follows a blackboard architecture: agents operate independently and communicate exclusively through structured Markdown artifacts in a shared workspace. That design drives the main engineering constraints in the runtime and artifact system.

- [docs/architecture/modular-orchestration-architecture.md](docs/architecture/modular-orchestration-architecture.md)
- [docs/architecture/adr/0001-ports-adapters-runtime-integrations.md](docs/architecture/adr/0001-ports-adapters-runtime-integrations.md)

## Adapter Authoring

1. Implement the relevant port contract under `src/orchestrator_cli/architecture/ports/`.
2. Register an alias in `src/orchestrator_cli/architecture/registry.py` or use a dotted path override in config.
3. Add adapter behavior tests under `tests/adapters/`.
4. Add architecture wiring tests under `tests/architecture/`.
5. Run quality gates before merge:
   - targeted adapter and architecture tests for changed integrations
   - `make lint`
   - `make format-check`
   - `make test`
