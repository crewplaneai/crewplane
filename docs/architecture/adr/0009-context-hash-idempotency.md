# ADR 0009: Workflow Signature Idempotency and Caching

## Status
Accepted

## Date
2026-04-10

## Decision
Utilize a coarse-grained, workflow-level signature strategy for run idempotency instead of implementing a fine-grained, dependency-aware drift detection engine. Each successful run writes a manifest (`{workflow_signature}.json`) under the run's `.orchestrator/execution-stages/<workflow>-<run_id>/manifests/` directory. Future runs that produce an identical successful workflow signature bypass the whole workflow unless `--force` is provided.

The signature is based on the compiled execution contract, not on presentation
state. It includes composed workflow semantics, static file hashes, env/var and
config fingerprints, provider execution settings, artifact-scoped integration
options, dependency graph identity, and execution-scoped runtime config.
Observer-only UI settings are excluded.

## Context
The architecture review identified "Run Idempotency / State" as a feature requirement to evaluate. AI workflows can be expensive and slow, so a mechanism is needed to suppress exact reruns. While enterprise build systems (like Bazel or Make) use dependency-aware incremental dirty-checking, this project uses input-based workflow signatures to provide immediate coarse-grained idempotency at the workflow-run boundary.

## Rationale
1. **Simplicity vs. Complexity**: Hashing the explicit execution context covers the vast majority (the 80/20) of use cases where nodes re-run needlessly. Building complex AST-based graph diffing or timestamp-based dependency tracking would heavily bloat the orchestrator core.
2. **Deterministic Inputs in a Non-Deterministic Environment**: AI endpoints are inherently non-deterministic. Ensuring idempotency by strictly hashing the complete input prompt, references, and configuration is the most robust way to guarantee the input hasn't mutated.
3. **Escapability**: This caching approach acts as a baseline that can easily be bypassed by users providing the `--force` flag when explicit re-runs are desired.

## Consequences
### Positive
- `OutputManager` remains relatively lightweight and highly predictable in its caching logic.
- Cost barriers to iterative workflow development are lowered because identical successful workflow runs skip execution automatically.

### Negative
- It is not a node-level incremental cache or complete drift detection system. A change to any hashed workflow input invalidates the whole run, and changes outside the captured context are not detected.

## Updates
- **2026-04-10**: Decision documented to formally capture the original context-hash implementation based on architecture review findings.
- **2026-06-03**: Updated by [ADR 0012](0012-preflight-compiled-runtime-execution-plan.md). `workflow_signature` replaces `context_hash` and includes compiled workflow semantics, static file hashes, env/var/config fingerprints, provider execution semantics, artifact-scoped options, and execution-scoped runtime config. Observer-only UI settings are excluded.
- **2026-06-07**: Folded in signature hardening. Sensitive fingerprints are
  scoped through preflight compile state rather than process-global key state,
  runtime config snapshots are classified by signature scope, and duplicate
  detection remains whole-workflow rather than node-level.
