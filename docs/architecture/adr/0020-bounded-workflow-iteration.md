# ADR 0020: Bounded Workflow Iteration

## Status

Accepted

## Date

2026-10-29

## Context

Some workflows must apply the same procedure to a collection whose size or
contents are known only after an earlier node finishes. Static imports alone
cannot express that work without knowing the collection before the run. At the
same time, Crewplane relies on preflight to validate workflows before provider
calls and on artifacts to reconstruct and reuse completed work after interruption.

This decision covers bounded iteration within one parent run. It extends the
compiled-plan and artifact-backed resume decisions in ADR 0012 and ADR 0014.
It does not introduce general runtime workflow authoring or nested iteration.

## Decision

Compile and validate a fixed child workflow during preflight, including its
imports, bindings, provider policies, resources, and limits. The child is
validated even when the collection is empty. Collection items are JSON data;
they do not supply workflow definitions or change the child procedure.

When a group becomes ready, the existing scheduler creates one instance of
the compiled child for each item. It validates the complete expansion and
records it before running the instances. The original plan remains unchanged;
the scheduler uses an effective plan that includes the committed instances.
The group finishes after its children succeed and publishes an ordered index
of their terminal results. Node concurrency continues to limit running work;
separate item and generated-node limits bound the size of the expansion.

The artifact layer records the collection and expansion evidence. Later
operations reconstruct the effective plan from the compiled child and verified
records rather than from event logs. Resume restores eligible expansions before
reusing generated successes or review checkpoints. Reuse also requires valid
prerequisites and matching caller artifacts. If selected evidence cannot be
restored, the run fails rather than silently starting provider calls again.

## Rationale

Compiling a fixed procedure preserves preflight validation and provider-neutral
runtime execution while allowing earlier work to supply the collection. Bounded
expansion limits resource use even when the collection is generated at runtime.
Recording the expansion before dispatch provides a durable description of the
generated graph for recovery, cleanup, and finalization. Checking dependencies
and caller artifacts separately from graph reconstruction prevents stale work
from being reused when its inputs change.

## Rejected Alternatives

- **Require authors to declare every instance before the run.** This preserves
  a fully static graph but cannot handle collections produced during execution.
- **Parse or generate child workflows at runtime.** This would let data change
  the procedure after preflight and weaken validation and execution identity.
- **Use event logs or in-memory scheduler state to recover the graph.** Events
  are for observation and may be incomplete; memory does not survive a run.
- **Start a separate run for each item.** This would split scheduling, results,
  and recovery across run boundaries instead of preserving the parent workflow.

## Consequences

### Positive

- Workflows can process bounded collections while retaining one run boundary,
  existing scheduling, and artifact-backed recovery.
- The generated graph and its inputs remain available for audit and recovery.

### Negative

- Runtime, artifact handling, and historical consumers must account for an
  effective graph that can grow after preflight.
- Expansion evidence takes storage and must be verified before reuse; invalid
  evidence can prevent recovery. Bounds and the ban on nested iteration limit
  workflows that need larger or recursively discovered collections.

## Related Decisions

- [ADR 0012: Preflight Compiled Runtime Execution Plan](0012-preflight-compiled-runtime-execution-plan.md)
- [ADR 0014: Artifact-Backed Node-Boundary Resume](0014-artifact-backed-node-boundary-resume.md)
