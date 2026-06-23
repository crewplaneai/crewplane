# Preflight And Idempotency

Every validate, dry-run, and real run compiles a preflight execution-plan
preview before providers are invoked.

For real execution, the runtime consumes the compiled preflight plan and bundle.
It does not reparse prompt templates or reread original `{{file:...}}` paths.

## What Preflight Compiles

Preflight resolves the composed workflow into:

- execution order
- execution nodes
- render plans
- static file resources
- Experimental workspace file locators
- token catalog entries
- dependency edges
- provider records
- redacted runtime config snapshot
- value fingerprints
- `workflow_signature`

Preflight diagnostics are emitted before runtime execution. A failed real-run
preflight writes failure artifacts so the run is auditable.

## Workflow Signature

Duplicate detection uses `workflow_signature`. The signature includes the
composed workflow, referenced workflow files, dependency graph, static file
content hashes, relevant env/var/config fingerprints, provider execution
settings, artifact-scoped integration options, and execution policy.

Observer-only UI settings do not determine the workflow signature.

## Duplicate Skip

With the built-in filesystem artifact backend, a successful previous run with
the same workflow identity and `workflow_signature` can make a later real run
skip instead of invoking providers again.

Use `--force` to bypass duplicate skip.

## Resume Boundary

When no valid same-context success exists, a failed or cancelled filesystem-backed
run can resume from validated completed node boundaries. The new execution gets a
fresh run directory, hydrates validated upstream results, and reruns unresolved
nodes.

`run --dry-run` only prints a resume advisory. It does not write run artifacts or
bind future execution to that advisory.
