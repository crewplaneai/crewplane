# Feature Overview

Orchestrator CLI is a declarative orchestration layer for AI coding assistants. It coordinates multiple providers through simple Markdown definitions, keeping every input, intermediate step, and final result transparent and auditable on disk.

## Declarative Infrastructure as Code

Modeled as "Control Plane for AI Workflows," execution is driven by vendor-neutral, declarative definitions rather than heavy SDKs, ensuring that all workflow intent and intermediate state remain fully transparent and manageable.

## Native AI CLI Orchestration

Integrates directly with existing native AI command-line tools installed on the host system. This completely eliminates the need for the orchestrator to manage API keys, secrets, or complex authentication layers, deferring trust directly to the underlying environment.

## Transparent Execution Artifacts

Guarantees full lifecycle auditability by isolating state directly to the filesystem. Every intermediate step is saved to dedicated stage directories (`.orchestrator/execution-stages/`) and every final outcome is consolidated into result directories (`.orchestrator/execution-results/`), providing a clear, trackable paper trail of the entire orchestration run.

## Workflow Composition & Reusability

Supports building modular, scalable orchestration graphs through imports, explicit parameterization, reusable inputs, and isolated naming spaces. This allows standard workflows to be shared securely without duplication.

## Workflow-Signature Idempotency

Built-in idempotency suppresses identical successful workflow runs. Preflight compiles a workflow-level signature from the composed workflow, referenced workflow files, dependency graph, static file content hashes, env/var/config fingerprints, provider execution settings, and execution/artifact-scoped integration options. A later run with the same `workflow_signature` is skipped unless `--force` is provided. Observer-only UI settings such as `--no-live` do not affect this signature.

## Node-Boundary Resume

When no valid same-context success exists, a failed or cancelled filesystem-backed run can resume from validated completed node boundaries. The resumed execution always gets a fresh run directory, hydrates only consolidated results and required findings artifacts, and reruns any unresolved or invalid frontier. `--force` bypasses both duplicate skip and resume hydration; `--dry-run` reports only advisory resume decisions.

## Robust Preflight Validation

Employs strict, fail-fast validation before execution begins. It compiles provider records, prompt render plans, dependency graph, static resources, token catalog, and a redacted runtime config snapshot before provider invocation; failures write diagnostics and summaries, while successful preflight writes a reusable execution bundle.

## Progressive UI Observability

Provides a dynamic terminal-based dashboard that automatically scales to the user's workspace. It utilizes objective metrics—like elapsed time and artifact growth—to convey liveness during long-running tasks, uses bounded formatted provider-log presentation when valid metadata exists, and still allows on-demand raw inspect of the exact persisted provider `.log`.

## Self-Healing State Management

Maintains operational resilience by cleanly recovering from damaged or incomplete local cache states. Corrupt run manifests or node-state records are treated as unusable history, safely prompting re-execution instead of skip or resume from untrusted state.

## Deterministic Simulation Engine

Includes a zero-cost mock execution adapter. This tool safely simulates workflow lifetimes, enforces custom output rules, and tests explicit failure conditions dependably without incurring network latency or vendor fees.

## Pluggable Architecture

Designed iteratively around explicit ports and adapters, allowing internal commands, execution targets, artifact storage, and UI dashboards to be securely replaced or expanded through configuration.
