# Experimental Worktree Implementation

This developer-facing guide explains how the Experimental workspace isolation
and worktree implementation behaves in real use. It expands the public
workspace guide with source policy, validation, runtime, artifact, retry,
cleanup, security, and troubleshooting details.

Start with [Experimental workspace isolation](../workspace-isolation.md) for
the short usage guide. See
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md)
for the minimal architecture decision.

## Who Should Read This

Read this guide when you are implementing, debugging, reviewing, or extending
the Experimental worktree implementation. It is written for contributors who
know Python and Git basics but may be new to Crewplane internals.

If you only want to run a workflow, use the shorter
[Experimental workspace isolation](../workspace-isolation.md) guide first.

## Mental Model

Workspace isolation keeps provider source edits out of the project root. A
workflow declares named workspaces with `worktrees`, and provider nodes select
one with `worktree: <name>`.

There are two workspace kinds:

- `worktree`: a mutable Git-backed source line. Successful nodes write
  `workspace-state.json` and `workspace.bundle` so later nodes can continue
  from the exact verified code state.
- `snapshot`: writable scratch space. Providers can write files there, but
  Crewplane discards those source changes and does not pass them downstream as
  code lineage.

Everything durable still goes through `.orchestrator/` artifacts. Live cache
directories and physical Git worktrees are temporary materializations, not the
source of truth.

## Key Terms

| Term | Meaning |
| --- | --- |
| `worktree` declaration | A workflow-level named workspace entry. |
| `worktree` node selector | A node field that chooses a declared workspace, or `none` to run at the project root. |
| Source line | The ordered chain of successful nodes that select the same `kind: worktree` name. |
| Lineage | Verified code state that can be used by downstream nodes. |
| `blob_exact` | The initial contract requiring provider-visible bytes to match Git blob bytes exactly. |
| `workspace-state.json` | The durable record of workspace source, result, setup, invocation, and cleanup facts. |
| `workspace.bundle` | A Git bundle artifact used to rehydrate verified code state later. |
| Managed workspace | A runtime-created worktree or snapshot cache directory outside the project root. |

## Reading Order

1. Read [Rationale](00-rationale.md) to understand why the feature exists.
2. Read [Boundaries, workflow model, and config model](01-boundaries-model-config.md) before editing schema, config, preflight, runtime, artifacts, or invoker code.
3. Read [Validation rules](02-validation-rules.md) and [Source policy and Git contract](03-source-policy-git-contract.md) before changing validation, Git probing, clean-start behavior, or unsupported-repository handling.
4. Read [Preflight and file template contracts](04-preflight-file-contract.md) before changing `{{file:...}}`, input nodes, prompt rendering, signatures, duplicate skip, or resume.
5. Read [Runtime, snapshot, and worktree strategies](05-runtime-strategies.md), [Retries, review loops, and invocation contract](06-retries-review-invocation.md), and [Result, bundle, fan-in, and state contracts](07-result-bundles-state.md) before changing execution behavior.
6. Read [Lifecycle, locking, and cleanup](08-lifecycle-locking-cleanup.md) before changing cache placement, Git locks, worktree removal, branch export, or cleanup commands.
7. Read [Resume, cancellation, security, performance, and UX](09-resume-cancellation-security-ux.md) before changing terminal-state behavior, diagnostics, summaries, or security-sensitive Git handling.
8. Use [Implementation milestones and tests](10-implementation-tests.md) as the checklist for regression coverage.

## Common Contributor Tasks

| Task | Start here |
| --- | --- |
| Add or change workflow syntax | [Boundaries, workflow model, and config model](01-boundaries-model-config.md) |
| Debug validation failures | [Validation rules](02-validation-rules.md) |
| Debug unsupported Git repositories | [Source policy and Git contract](03-source-policy-git-contract.md) |
| Change file-token behavior | [Preflight and file template contracts](04-preflight-file-contract.md) |
| Change provider `cwd` or invocation metadata | [Retries, review loops, and invocation contract](06-retries-review-invocation.md) |
| Change snapshot or worktree provisioning | [Runtime, snapshot, and worktree strategies](05-runtime-strategies.md) |
| Change result capture or bundles | [Result, bundle, fan-in, and state contracts](07-result-bundles-state.md) |
| Change cleanup | [Lifecycle, locking, and cleanup](08-lifecycle-locking-cleanup.md) |
| Add tests | [Implementation milestones and tests](10-implementation-tests.md) |

## Detailed Sections

- [Rationale](00-rationale.md)
- [Boundaries, workflow model, and config model](01-boundaries-model-config.md)
- [Validation rules](02-validation-rules.md)
- [Source policy and Git contract](03-source-policy-git-contract.md)
- [Preflight and file template contracts](04-preflight-file-contract.md)
- [Runtime, snapshot, and worktree strategies](05-runtime-strategies.md)
- [Retries, review loops, and invocation contract](06-retries-review-invocation.md)
- [Result, bundle, fan-in, and state contracts](07-result-bundles-state.md)
- [Lifecycle, locking, and cleanup](08-lifecycle-locking-cleanup.md)
- [Resume, cancellation, security, performance, and UX](09-resume-cancellation-security-ux.md)
- [Implementation milestones and tests](10-implementation-tests.md)
- [Rejected alternatives and consequences](11-alternatives-consequences.md)
