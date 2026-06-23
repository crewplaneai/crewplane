# ADR 0005: Deeper Workflow Validation in Preflight

## Status
Accepted

## Date
2026-04-10

## Decision
Enhance the `crewplane validate` command to perform deep preflight validation matching the `run` command's behavior. This includes:
- Checking provider existence in the resolved configuration.
- Verifying the presence of provider CLI executables on the system `PATH`.
- Validating external template references (`{{file:...}}`, `{{env:...}}`, `{{var:...}}`) using the shared preflight logic.
- Reusing the same structured workflow diagnostics, workflow syntax constants,
  provider resolution checks, token-budget checks, file-access checks, and
  env/var template checks used by run preflight.
- Emitting warning diagnostics for explicit argv prompt transport because it can
  expose rendered prompt content in process arguments.
- Preserving boundary validation for duplicate-key YAML rejection, schema
  versions, duplicate agent names, exact keyword sets, command token checks,
  retry/quota policy checks, pricing checks, audit-round checks, and configured
  provider resolution.

## Context
The `crewplane validate` command previously checked only the markdown shape, schema conformity, DAG dependency validity, and output-reference topology. However, the runtime preflight phase additionally checked provider availability and template reference resolution.
Because `validate` did not execute these same runtime checks, workflows could pass Continuous Integration (CI) validation but still fail during an actual `run`.

## Rationale
1. **Trust in CI:** Enterprise CI pipelines need to run `crewplane validate` as a comprehensive pre-merge gate, ensuring that the merged workflows are highly likely to execute successfully. This was identified as a high-priority Phase 1 requirement.
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
- **2026-04-10**: Implemented the shared preflight validation logic across both the `run` and `validate` Typer commands in `app.py`, backed by the deeper checks in `workflow/validation/api.py`.
- **2026-06-07**: Folded in validation boundary hardening. Workflow syntax and
  keyword constants are centralized, validation emits structured diagnostics,
  and preflight delegates to the shared diagnostic path rather than duplicating
  structural checks.
