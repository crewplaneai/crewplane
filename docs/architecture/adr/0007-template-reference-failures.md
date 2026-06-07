# ADR 0007: Fail Fast on Template Reference Failures

## Status
Accepted

## Date
2026-04-10

## Decision
Enforce strict fail-fast behavior during preflight when external template references (`{{file:...}}`, `{{env:...}}`, `{{var:...}}`) cannot be properly resolved.
If an externally referenced file is missing, blocked, or invalid, or if an environment variable or template variable is not set, the workflow execution will immediately halt with a clear error message.

## Context
Previously, template reference resolution was more lenient or handled during runtime execution. This could lead to partial workflow executions, where downstream nodes might be invoked with raw, unresolved template placeholders (or error placeholders), causing subtle failures, wasted compute, or undefined behaviors in the orchestrator tools. By the time the error was apparent, time and API costs were already expended on earlier nodes. This addresses: "Template Reference Failures During Run" from the architecture review.

## Rationale
1. **Predictable Execution:** A workflow execution cannot be considered valid if its declared parameters or file dependencies are missing. 
2. **Cost and Time Savings:** Failing fast prevents the LLM API from being called with garbage data or placeholder strings.
3. **Better DX:** Immediate validation surfaces environment configuration issues, missing files, or path typos directly to the user before they wait for AI steps to complete.

## Consequences
### Positive
- Prevents silent prompt corruption and failed generation attempts.
- Consolidates error reporting so all template errors are displayed upfront.
- Improved trust in the workflow system, as users know execution only begins if prerequisites are met.

### Negative
- Workflows that intentionally rely on missing variables resolving to empty strings or placeholders will now break and require explicit passing or conditionals. 
- Requires correct environment state setup before executing even dry-runs or partial executions.

## Updates
- **2026-04-10**: Implemented shared runtime-template preflight validation before scheduling any execution waves.
- **2026-06-03**: Superseded operationally by [ADR 0012](0012-preflight-compiled-runtime-execution-plan.md). Core preflight now compiles file/env/var references into a deterministic execution plan and static bundle, and runtime no longer owns template resolution.
