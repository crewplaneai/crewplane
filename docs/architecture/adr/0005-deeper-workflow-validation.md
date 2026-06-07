# ADR 0005: Deeper Workflow Validation in Preflight

## Status
Accepted

## Date
2026-04-10

## Decision
Enhance the `orchestrator validate` command to perform deep preflight validation matching the `run` command's behavior. This includes:
- Checking provider existence in the resolved configuration.
- Verifying the presence of provider CLI executables on the system `PATH`.
- Validating external template references (`{{file:...}}`, `{{env:...}}`, `{{var:...}}`) using the shared preflight logic.

## Context
The `orchestrator validate` command previously checked only the markdown shape, schema conformity, DAG dependency validity, and output-reference topology. However, the runtime preflight phase additionally checked for provider availability and template resolution readiness.
Because `validate` did not execute these same runtime checks, workflows could pass Continuous Integration (CI) validation but still fail during an actual `run`.

## Rationale
1. **Trust in CI:** Enterprise CI pipelines need to run `orchestrator validate` as a comprehensive pre-merge gate, ensuring that the merged workflows are highly likely to execute successfully. This was identified as a high-priority Phase 1 requirement.
2. **Eliminate Drift:** Utilizing a single shared validation path between `run` and `validate` prevents behavioral drift and duplication of logic across commands.
3. **Fail Fast:** Detecting missing CLI executables, malformed configurations, or missing environment variables early reduces wasted execution time.

## Consequences
### Positive
- Higher confidence in workflow correctness before merge.
- Unified preflight logic reduces maintenance burden and simplifies testing.
- Immediate feedback in the CLI when referencing missing tools or configuration.

### Negative
- `validate` now expects the validation environment (PATH, env vars, configured tools) to more closely mirror the execution environment, which might require structural updates to CI runners.

## Updates
- **2026-04-10**: Implemented the shared preflight validation logic across both the `run` and `validate` Typer commands in `app.py`, backed by the deeper checks in `workflow_validation.py`.
