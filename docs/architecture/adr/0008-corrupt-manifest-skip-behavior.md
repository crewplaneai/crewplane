# ADR 0008: Corrupt Manifest Skip Behavior

## Status
Accepted

## Date
2026-04-10

## Decision
Treat corrupt, malformed, or unreadable execution manifest files as non-existent (effectively returning `False` for the cache check), thereby bypassing identical-workflow-signature suppression and causing the workflow to re-run.

Manifest corruption remains a cache miss, not a partial trust state. Preflight
failure artifacts do not write final execution manifests and cannot trigger
future duplicate skips.

## Context
When the orchestrator executes a workflow, it saves current run state in `/manifests/run.json` with the compiled `workflow_signature`. If a future run produces the same successful signature, the system suppresses execution to save time and API costs (idempotency/caching).
Previously, if stored manifest JSON was corrupted (e.g., due to a process crash during write, manual editing, or disk issues), the system might crash or incorrectly assume the run should be skipped, leaving the workflow in an unrecoverable "skipped but broken" state. This addresses: "Corrupt Manifest Skip Behavior" from the architecture review.

## Rationale
1. **Safety First:** A corrupted manifest means the state of the outputs cannot be trusted.
2. **Self-Healing:** By treating a malformed manifest as a cache miss, the system naturally heals itself on the next run by repeating the execution and safely overwriting the corrupted JSON state upon success.
3. **Resilience:** The orchestrator execution engine should not crash completely just because an old artifact file is unreadable.

## Consequences
### Positive
- Workflow execution gracefully recovers from interrupted writes or corrupted state directories.
- Users no longer need to manually clear the `.orchestrator/execution-stages/` directory to recover from a broken JSON manifest.

### Negative
- A user might experience a full re-run (and associated LLM costs) unexpectedly if a manifest file becomes corrupted, though this is preferable to a silent failure.

## Updates
- **2026-04-10**: Implemented robust exception handling for corrupt persisted
  execution manifests during duplicate detection.
- **2026-06-03**: Updated by [ADR 0012](0012-preflight-compiled-runtime-execution-plan.md). Manifest lookup is now keyed by `workflow_signature` instead of the original context hash.
- **2026-06-07**: Folded in manifest hardening. Manifests remain metadata-only,
  signed preflight/runtime plan verification remains part of the execution
  contract, and corrupt final manifests continue to force a rerun rather than a
  skip.
- **2026-06-09**: Corrupt current-layout `run.json` or node-state records are
  ignored for skip/resume decisions. Legacy signature-keyed manifests are no
  longer part of the supported run artifact layout.
