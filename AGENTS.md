# AGENTS.md

Canonical repository instructions for coding agents.

Use this file for repo-wide agent behavior. Use [DEVELOPMENT.md](./DEVELOPMENT.md) for broader engineering context and maintenance notes. More specific `AGENTS.md` files in subdirectories override this file for their subtree.

## Repo Facts

`orchestrator-cli` is a Python 3.13+ Typer CLI for running multi-agent workflows defined in Markdown. The core architectural rule is blackboard-style orchestration: providers do not coordinate through shared in-memory state; they communicate through artifacts written under `.orchestrator/`.

When changing behavior, preserve these properties:

- CLI-first provider integration. The project is built around external AI CLIs, not vendor SDKs.
- Auditable execution. Inputs, intermediate outputs, manifests, and results stay on disk as readable files.
- Explicit boundaries. Config loading, workflow parsing/composition, adapter resolution, provider invocation, runtime execution, artifacts, and observability are separated on purpose.
- Deterministic validation. New behavior should be covered by filesystem-local pytest tests, and mock-driven end-to-end checks should stay available.

## Read First

- `README.md`: short package and repository orientation
- `docs/index.md`: full public user documentation
- `DEVELOPMENT.md`: human-facing setup, quality gates, repo layout, and local validation workflows
- `docs/architecture/index.md`: architecture entry point and ADR links

## Repo Map

- `src/orchestrator_cli/cli/`: Typer command surface, task/config path resolution, run orchestration, cleanup commands, scaffold templates
- `src/orchestrator_cli/core/`: config models, workflow schemas, Markdown parsing, workflow composition/imports, DAG validation, preflight compilation, schema version validation
- `src/orchestrator_cli/architecture/`: port contracts, integration loader, alias registry, adapter errors
- `src/orchestrator_cli/bootstrap/`: composition root that wires configured adapters into runtime components
- `src/orchestrator_cli/runtime/`: provider invocation, retry/quota handling, parallel/sequential workflow execution
- `src/orchestrator_cli/artifacts/`: stage/result directories, manifests, generated files, locks, result writing, resume, workspace state, output access
- `src/orchestrator_cli/observability/`: event model, runtime snapshots, rendering, tmux support
- `src/orchestrator_cli/adapters/`: built-in invoker, UI, and artifact implementations
- `src/orchestrator_cli/example_templates/`: files used by `orchestrator init`
- `tests/`: CLI, config, workflow, runtime, observability, adapter, and architecture coverage

## Project Heuristics

- Keep modules cohesive and strongly typed. Validate data at boundaries and raise explicit errors.
- Keep provider-specific invocation transport inside invoker adapters or adapter-owned invoker capability modules. Runtime execution should consume provider-agnostic invoker contracts and should not infer provider behavior from executable names, CLI flags, output formats, quota text, or usage text.
- Do not introduce hidden cross-node state. Downstream behavior should continue to derive from workflow definitions, config, and artifact files.
- Do not weaken template path restrictions casually. `{{file:path}}` is intentionally bounded to the project root unless explicitly allowlisted through `allowed_template_paths`.
- Prefer deleting dead code over leaving compatibility shims, stale branches, or commented-out removals.
- Keep comments rare and high-signal. Explain why a choice exists, not what the code already states.
- Prefer simple, local designs over premature abstraction. Add indirection only when the boundary is already real in the architecture.

## Coding Standards

- Use explicit type hints for public APIs and non-trivial logic.
- Validate inputs at system boundaries: CLI, config, workflow files, template resolution, and adapter interfaces.
- Fail explicitly on invalid state. Do not swallow exceptions or silently continue when correctness is at risk.
- Keep functions focused and readable. Make illegal states hard to represent.
- New behavior requires deterministic tests. Bug fixes require regression coverage.
- Avoid hidden fallback behavior unless it is deliberate, documented, and covered by tests.

## Change Guidance

### CLI surface

- Main entrypoint: `src/orchestrator_cli/cli/app.py`
- Supporting run flow: `src/orchestrator_cli/cli/run/` plus the `src/orchestrator_cli/cli/workflow_runner.py` facade
- Cleanup command surface: `src/orchestrator_cli/cli/cleanup.py`
- Path resolution and scaffold helpers: `src/orchestrator_cli/cli/paths.py`, `src/orchestrator_cli/cli/templates.py`
- Expected tests: `tests/integration/cli/`, plus any affected unit tests under `tests/unit/`

If command output, validation rules, scaffold files, or default behavior changes, update docs and example templates in the same change.

### Workflow schema, parsing, and composition

- Core files: `src/orchestrator_cli/core/workflow_models.py`, `src/orchestrator_cli/core/workflow_markdown/`, `src/orchestrator_cli/core/workflow_loader.py`, `src/orchestrator_cli/core/workflow_composition/`, `src/orchestrator_cli/core/workflow_validation*.py`, `src/orchestrator_cli/core/preflight/`
- Expected tests: `tests/unit/core/workflow_loading/`, `tests/unit/core/workflow_composition/`, `tests/unit/core/workflow_validation/`, `tests/unit/core/preflight/`, and relevant `tests/integration/cli/` coverage

Important invariants:

- Workflow schema version must match `src/orchestrator_cli/version.py`
- Markdown workflows require one `## <node-id>` section per frontmatter node
- Imports are Markdown-only, alias-namespaced, and must stay within `Path.cwd()`
- `{{param:key}}` is composition-time only; unbound params are rewritten to `{{var:key}}`
- `{{node.output}}` references should only be valid for upstream dependencies

### Config and provider invocation

- Core config: `src/orchestrator_cli/core/config.py`, `src/orchestrator_cli/core/config_workspace.py`, `src/orchestrator_cli/core/token_budget.py`
- Runtime invoker path: `src/orchestrator_cli/runtime/agent/`
- Built-in invokers: `src/orchestrator_cli/adapters/invokers/`
- Expected tests: `tests/unit/core/test_config.py`, `tests/integration/runtime/agent/`, `tests/integration/adapters/test_invoker_cli.py`, and `tests/integration/adapters/mock_invoker/`

Keep retry, quota, command-building, prompt transport, output parsing, and usage parsing behavior explicit. Provider-specific rules belong behind the invoker adapter boundary or in shared invoker capability modules owned by that boundary. If you add a provider-specific parsing rule or retry condition, add regression coverage for both positive and failure paths.

### Runtime execution

- Workflow scheduler: `src/orchestrator_cli/runtime/execution/workflow/__init__.py`
- Stage execution: `src/orchestrator_cli/runtime/execution/parallel.py`, `src/orchestrator_cli/runtime/execution/sequential.py`, `src/orchestrator_cli/runtime/execution/consensus.py`
- Expected tests: `tests/integration/runtime/execution/`, `tests/integration/cli/test_workflow_runner.py`, and affected `tests/unit/runtime/` coverage

Preserve DAG semantics, manifest dedupe behavior, `--force` override behavior, and the distinction between node concurrency and per-invocation concurrency.

### Adapters and architecture boundaries

- Port contracts: `src/orchestrator_cli/architecture/ports/`
- Alias registry: `src/orchestrator_cli/architecture/registry.py`
- Loader: `src/orchestrator_cli/architecture/loader.py`
- Composition root: `src/orchestrator_cli/bootstrap/container.py`
- Expected tests: `tests/integration/architecture/`, relevant `tests/integration/adapters/`

For a new integration or adapter change:

1. Update the relevant port contract or adapter implementation.
2. Register the alias in `registry.py` unless a dotted-path-only integration is intentional.
3. Ensure `loader.py` and `bootstrap/container.py` still wire the adapter correctly.
4. Add or update adapter behavior tests and architecture wiring tests.

Port contracts should remain stable extension boundaries. Avoid importing concrete runtime execution or observability implementations into `architecture/ports/`; move shared DTOs or protocol data into architecture-level or core-neutral modules when a port needs them.

### Artifacts, manifests, and templates

- Core files: `src/orchestrator_cli/artifacts/manager.py`, `src/orchestrator_cli/artifacts/directory_manager.py`, `src/orchestrator_cli/artifacts/generated_files/`, `src/orchestrator_cli/artifacts/locks/`, `src/orchestrator_cli/artifacts/results/`, `src/orchestrator_cli/artifacts/resume/`, `src/orchestrator_cli/artifacts/workspace/`, and `src/orchestrator_cli/core/preflight/`
- Built-in implementation: `src/orchestrator_cli/adapters/artifacts/filesystem.py`
- Expected tests: `tests/unit/artifacts/`, `tests/integration/adapters/test_artifacts_filesystem.py`, and affected `tests/integration/cli/` coverage

The implementation uses hyphenated output directories:

- `.orchestrator/execution-stages/`
- `.orchestrator/execution-results/`

Keep new docs and code aligned to those paths.

### Observability and tmux UI

- Core files: `src/orchestrator_cli/observability/`, `src/orchestrator_cli/adapters/ui/`
- Expected tests: `tests/integration/observability/`, `tests/unit/observability/`, `tests/integration/adapters/test_ui_tmux.py`, `tests/integration/adapters/test_ui_null.py`

The live UI must degrade cleanly:

- `--no-live` should leave execution fully functional
- missing tmux should not break runs
- tmux live mode depends on artifact log capture being enabled

## Validation Expectations

Use `make` targets first; they already handle `uv` when available and fall back to `python -m ...` otherwise.

```bash
make setup
make test
make lint
make format
make format-check
make check
```

Useful targeted test runs:

```bash
uv run --extra dev python -m pytest -q tests/integration/cli/test_workflow_discovery_and_init.py
uv run --extra dev python -m pytest -q tests/unit/core/workflow_composition tests/unit/core/workflow_validation
uv run --extra dev python -m pytest -q tests/integration/adapters/mock_invoker tests/integration/architecture/test_container.py
```

If `uv` is unavailable, run the same targeted paths with `python -m pytest -q`
after `make setup`.

For end-to-end validation without provider CLI calls, prefer the `mock` invoker in `.orchestrator/config.yml`.

Useful commands:

```bash
orchestrator init
orchestrator validate
orchestrator run --dry-run
orchestrator run --no-live
```

When validating behavior manually, inspect artifacts under `.orchestrator/execution-stages/` and `.orchestrator/execution-results/` and confirm manifest dedupe behavior still matches the intended `workflow_signature` rules.

## Documentation Expectations

Keep docs synchronized with implementation, especially when changing:

- CLI flags or defaults
- config keys or schema versions
- workflow composition/import behavior
- artifact directory layout
- built-in integration names or options
- generated example templates

- Keep instructions specific, consistent, and compact. Remove or rewrite outdated guidance instead of layering conflicting rules.
- Keep shared repository rules here. If guidance only applies to a narrower area, place it closer to that scope instead of expanding this file indefinitely.
- If instructions grow large, split them by scope so the most relevant guidance stays nearest to the work it controls.
