# Modular Orchestration Architecture

## Context
Orchestrator CLI is a DAG orchestration layer for AI tooling. The product goal is stable orchestration semantics with replaceable runtime integrations (CLI today, LLM APIs and web UI later).

The architecture in this repo follows a ports-and-adapters model:
- Core orchestration logic remains fixed.
- Runtime integrations are swapped through explicit contracts.

## Goals
1. Preserve DAG execution behavior.
2. Allow runtime integrations to be replaced without editing orchestration core code.
3. Keep configuration deterministic and explicit.
4. Expose a stable extension surface for open-source contributors.

## Non-Goals
1. Plugin marketplace/discovery in this phase.
2. Dynamic external entry point loading in this phase.
3. Changing workflow schema semantics.
4. Shipping first-party LLM API or web UI implementations in this phase.

## Architecture Overview

### Core (Non-Replaceable)
- `runtime/execution/*`: scheduling, sequential/parallel semantics, failure thresholds, consensus flow.
- `core/workflow_*`: workflow parsing, import composition, graph validation, schema checks.

### Replaceable Integrations (Adapters)
- Invoker adapter: how provider calls are executed.
- UI adapter: observer-only live run visualization.
- Artifacts adapter: stage/result/log/manifests/template resolution storage layer.

Current built-in invokers are:
- `cli` for real provider CLI execution
- `mock` for deterministic, cost-free local validation

Current built-in `tmux` UI uses a compact two-pane dashboard with an on-demand raw log inspect mode and runs as observer-only (no invoker override).

### Contracts (Ports)
Stable contracts are under `orchestrator_cli.architecture.ports`:
- `ArtifactStorePort`
- `InvokerAdapterPort`
- `UIAdapterPort`
- `UIRuntimePlan`
- `RuntimeComponents`

`orchestrator_cli.version.SCHEMA_VERSION` is the source of truth for current config, workflow, and preflight artifact shape validation.

## Configuration Model
The current config schema version is authored in `orchestrator_cli.version`. Config uses `settings.integrations`:

```yaml
agents:
  codex:
    cli_cmd: ["codex", "exec"]
    provider_kind: "codex"
    prompt_transport: "stdin"
    prompt_transport_arg: "-"

settings:
  integrations:
    invoker:
      implementation: "cli"
      options: {}
    ui:
      implementation: "tmux"
      options:
        auto_close_session: true
        quiet_after_seconds: 120.0
        # log_tail_lines: 40 # optional fixed cap; omit or set null to fit the right pane height
    artifacts:
      implementation: "filesystem"
      options:
        log_cli_output: true
        allowed_template_paths: []
```

tmux UI option intent:
- `quiet_after_seconds`: when a running invocation has not appended new log output for this long, render quiet-state liveness messaging in the right pane.
- `log_tail_lines`: optional fixed cap for tailed log lines in compact dashboard mode; omit it or set it to `null` to fit the right pane height automatically.
- `Enter` opens the selected node's raw log in the same right pane, and that inspect mode stays locked to the chosen log until `Esc`.

## Implementation Resolution Strategy
Resolution is alias-first with dotted-path override:
1. Alias (`cli`, `mock`, `tmux`, `none`, `filesystem`) resolves through internal registry.
2. Dotted path (`package.module:ClassName` or `package.module.ClassName`) loads directly.

Why this was chosen:
- Explicit and deterministic in production.
- Easy to debug and document.
- No extra runtime dependency.
- Supports internal and external implementations immediately.

## Why We Deferred Entry Points and Pluggy
### Entry-point discovery deferred
- Useful when there is an external plugin ecosystem.
- Adds packaging/discovery lifecycle overhead.
- Deferred until multiple external adapter packages exist and warrant it.

### Pluggy deferred
- Hook systems are valuable for broader extension surfaces.
- Current requirement is adapter replacement at known boundaries, not generic hook dispatch.
- Would add dependency and conceptual overhead with low immediate return.

## Failure Policy
1. Invoker/artifacts adapter load or contract failure: hard fail.
2. UI adapter load/contract failure: hard fail.
3. tmux unavailable when using tmux UI adapter: warn and continue without live dashboard.
4. Live observer startup failure at runtime: warn and continue with base invoker.

## Core vs Replaceable Boundary
### Core responsibilities
- Node dependency ordering.
- Parallel/sequential execution semantics.
- Retry/quota behavior inside invoker implementation.
- Workflow-level success/failure aggregation.
- Review-loop canonical-candidate validation, remediation no-progress handling, artifact-drift detection, and review-state/status persistence.
- Workflow composition (`imports`, namespace rewrite, cycle/collision checks, `{{param:...}}` substitution, declared workflow inputs, and import-time input binding rewrite).

### Replaceable responsibilities
- Invocation transport (CLI/API/etc).
- Runtime live visualization and interaction model.
- Artifact storage and template resolution implementation.

## Review-Loop Integrity

Sequential executor/reviewer loops rely on runtime-owned integrity checks rather than adapter-specific permission systems:

- `runtime/execution` chooses the canonical current-round candidate set before reviewers run.
- The runtime skips reviewer calls only for high-confidence invalid candidates or unchanged remediation candidates.
- Artifact drift is detected by snapshotting stage/run artifacts around provider calls.
- Drift inside the current node stage tree is warning-level.
- Shared execution results, manifests, and other reserved run-root log artifacts are treated as fatal only when the invocation was isolated enough that the runtime can attribute those changes safely.
- `logs/summary.md` is always treated as a strict reserved artifact. When an invocation is attributable, `logs/events.ndjson` may only gain event records emitted by that guarded runtime invocation; concurrent node windows fall back to destructive-drift detection to avoid blaming other nodes' legitimate runtime events. Parallel reviewers inside one node share an event capture so their runtime records remain exactly attributable.
- Review-loop nodes persist `review-state/review-loop-status.json`, and `artifacts/result_writer.py` uses that status artifact to finalize results instead of inferring the winner from lexically latest filenames.

This keeps the blackboard contract explicit without adding a second config- or adapter-level permission model.

## Workflow Composition Boundary

Workflow composition is intentionally implemented in `core/workflow_*` so adapter contracts remain stable:
- Imports are resolved before runtime execution and before observer/layout rendering.
- Core preflight compiles the composed workflow into a `PreflightExecutionPlan` before runtime execution.
- Runtime execution consumes the compiled plan, an artifact store, runtime services, and same-process secret handles; it does not parse prompt tokens or re-read file-token source paths.
- UI adapters receive observer-only topology data derived from the plan and are not allowed to alter provider invocation.
- Bound imported input nodes are pruned during composition, so runtime and UI layers see only the executable composed DAG plus any unbound file-backed input roots that remain standalone.
- `{{file:...}}`, `{{env:...}}`, `{{var:...}}`, and file-backed `mode: input` sources are compiled by core preflight into ordered prompt fragments, static resources, fingerprints, and runtime locators.
- Imported workflows must match the root workflow `schema_version`; mismatches fail during core parsing/composition.

## Extending This Architecture
### LLM API invoker adapter (future)
Implement `InvokerAdapterPort.create_invoker(...)` and return an `AgentInvoker` compatible object.

### Web UI adapter (future)
Implement `UIAdapterPort.create_runtime(...)` and return observer-only runtime UI components.

### Alternative artifact adapter (future)
Implement side-effect-free artifact option canonicalization plus the `ArtifactStorePort` surface via `ArtifactAdapter.create_store(...)` for concrete workflow runs.

Artifact-backed duplicate skip, same-context locking, run-history scan, and
node-boundary resume are filesystem-only in v1. Real execution with another
artifact backend fails before lock/skip/resume/full-run semantics are applied;
`validate` and `run --dry-run` remain side-effect-free advisory paths.

## Compatibility and Migration
- Schema version policy is documented in `DEVELOPMENT.md` and [ADR 0013](adr/0013-version-source-of-truth-and-documentation-drift-reduction.md).
- The current schema version is authored in `orchestrator_cli.version`.
- Users should regenerate config via `orchestrator init` when templates change.

## Operational Guidance
- Keep new integration implementations constrained to port contracts.
- Avoid leaking adapter-specific concerns into workflow schema.
- Add contract-focused tests for every new adapter implementation.
