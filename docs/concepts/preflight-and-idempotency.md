# Preflight, Duplicate Skip, and Resume

Preflight is how Crewplane knows what it is about to run before provider CLIs
start. It is also the basis for duplicate-skip and safe resume decisions.

Every validate, dry-run, and real run compiles a preflight execution-plan
preview before providers are invoked.

For real execution, the runtime consumes the compiled preflight plan and bundle.
It does not reparse prompt templates or reread original `{{file:...}}` paths.

```text
validate / dry-run / run
        |
        v
compile preflight plan
        |
        v
compute workflow_signature
        |
        v
run providers or decide skip/resume
        |
        v
write manifests and results
```

## What Preflight Compiles

Preflight resolves the composed workflow into:

- execution order
- execution nodes
- render plans
- static file resources
- workspace source metadata
- workspace file locators
- token catalog entries
- dependency edges
- provider records
- redacted runtime config snapshot
- effective runtime config signature
- value fingerprints
- fingerprint metadata
- `workflow_signature`

Preflight diagnostics are emitted before runtime execution. A failed real-run
preflight writes failure artifacts so the run remains inspectable.

## Workflow Signature

Duplicate detection uses `workflow_signature`. The signature includes the
composed workflow, referenced workflow files, dependency graph, static file
content hashes, workspace file locator facts, relevant env/var/config
fingerprints, provider execution settings, artifact-scoped integration options,
and execution policy.

Observer-only UI settings do not determine the workflow signature. Branch-export
fields are intentionally excluded because they affect how verified workspace
results are exposed, not what providers run. The default workspace cache root is
excluded unless `settings.workspace.identity.include_cache_root` is `true`.

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
