# ADR 0021: Frozen Recall and Reviewed Project Learning

## Status

Accepted

## Date

2026-11-01

## Context

Reusable lessons need a project-local lifecycle without becoming hidden mutable
input to an executing workflow. Crewplane compiles before choosing history,
executes frozen plans, and coordinates providers through artifacts. A successful
run can also contain partial failures or unresolved reviews, so its outcome
cannot establish that every proposed lesson is correct.

Learning must remain explicit and independently inspectable without changing
parent dependency, failure, cancellation, or observer semantics.

## Decision

Provider nodes opt in through workflow declarations resolved during composition.
The CLI supplies one accepted-memory snapshot per compilation. Core selects
lessons for each provider role from eligible inputs captured before execution.
Preflight fixes those selections as text, not workflow syntax. Generated children
reuse their template's selection without another store lookup. Selected lessons
and their rendered text form part of the workflow signature used for reuse.
Unrelated store state, timestamps, inspection diagnostics, and learning-only
policy do not; adapter-registration identity remains intact.

Crewplane saves selection receipts beside the execution plan and checks that the
receipts and plan agree before execution or reuse, without consulting the live
store. Accepting, correcting, or forgetting a lesson later does not change an
active plan. The shared runtime-state file/source policy excludes mutable memory.
The following rules govern workflow completion and any subsequent lesson
extraction:

- After Crewplane saves the parent workflow's outcome, completes the required
  shutdown and cleanup, confirms that the execution-lock handle released the
  lock, and displays the result, the CLI may run a separate packaged workflow
  to extract possible lessons.
- The extraction workflow is compiled separately and uses the usual invoker,
  retry, quota, artifact, and cleanup mechanisms. Memory and learning are
  disabled for extraction.
- Crewplane supplies the extractor with a limited packet of verified retained
  evidence as text. Crewplane assigns the evidence identities and records any
  limitations of the parent outcome; the extractor cannot manufacture them.
- Ordinary learning failures leave the parent outcome unchanged. If cleanup
  cannot be confirmed, the command fails.
- For executed runs with memory enabled, including recall-only runs, Crewplane
  retains a completion receipt tied to the run's identity at
  `manifests/completion.json`. A later explicit extraction requires this proof
  of completion. A terminal manifest and a missing lock cannot establish that
  cleanup was confirmed. The receipt does not change the parent workflow's
  final outcome.

Extraction imports candidates, never acceptance, and preserves mandatory
parent-outcome limitations in retained evidence owned by Crewplane. Human review
binds each decision to an exact revision, content fingerprint, and accepted predecessor. Edits create new immutable
candidates. Selected decisions commit atomically or not at all; pending corrections
leave recall unchanged. Repeated passes collect candidates and review once after
the sequence. Noninteractive execution leaves candidates pending.

Project memory uses a local store with a lock to coordinate access:

- **Storage:** Memory is kept in a size-limited JSON store.
- **Lock:** A separate, persistent `fcntl.flock` lock coordinates access on
  supported Linux, macOS, and WSL systems.
- **Initialization:** Only initialization creates the store and lock.
- **Writes:** Crewplane takes an exclusive lock, reloads the store, checks that
  the required conditions still hold, and saves all changes atomically.
- **Reads:** Crewplane holds the lock briefly to take a single snapshot.
- **Long-running actions:** Model calls, editors, and human prompts do not hold
  the lock.
- **Uncertain saves:** If saving fails after the store may have been replaced,
  inspect the current state before retrying.

## Rationale

Freezing recalled lessons before execution preserves a reproducible execution
identity and artifact-backed reuse. Running extraction separately from the parent
workflow makes the extraction's cost, outcome, deadline, and failure policy
explicit. Tying each review decision to a specific revision separates Crewplane's
responsibility for evidence integrity from human judgment. It also prevents a
stale approval from a concurrent review from replacing an accepted revision the
reviewer has not seen. Using JSON for the small, size-limited memory store keeps
it easy to inspect without introducing a storage service or a new adapter family.

## Rejected Alternatives

- **Runtime or per-item retrieval:** changes inputs after preflight and makes
  duplicate skipping, child-template identity, and resume dependent on mutable state.
- **A synthetic final parent node:** couples optional learning to the parent's
  graph and failure semantics.
- **Observer callbacks or a background service:** give observation business
  effects or introduce a second lifecycle when the CLI already owns completion.
- **Provider-native memory:** does not supply a shared project-local review and
  execution-identity contract across invokers.
- **SQLite, embeddings, or semantic merging now:** add machinery not required by
  the bounded lexical corpus and explicit revision lifecycle. Curated Markdown
  remains an alternative for maintained authoritative guidance.

## Consequences

- Runs retain inspectable recall and learning evidence, with explicit opt-in and
  no change to the default mock-first workflow.
- Recall can miss relevant lessons or select stale ones. Runtime-only task inputs
  cannot guide preflight retrieval, and child instances share template selection.
- Retained revisions consume bounded storage. Full-record rejection at the limit
  is preferable to silent history trimming. Advisory locks coordinate cooperating
  processes, not arbitrary same-user writers.
- Extraction adds provider work and may expose sensitive task/output content
  before human review. Acceptance and historical labeling are not security
  boundaries or prompt-injection defenses.
- Forgetting removes future recall, not historical plans, logs, backups, or active
  invocations. Evidence claims and captured verification remain distinct.

## Related Decisions

- [ADR 0010: Core Positioning](0010-core-positioning-infrastructure-as-code.md)
- [ADR 0011: Review Execution Workflow Optimization](0011-review-execution-workflow-optimization.md)
- [ADR 0012: Preflight Compiled Runtime Execution Plan](0012-preflight-compiled-runtime-execution-plan.md)
- [ADR 0014: Artifact-Backed Node-Boundary Resume](0014-artifact-backed-node-boundary-resume.md)
- [ADR 0019: Explicit Plugin System](0019-explicit-plugin-system.md)
- [ADR 0020: Bounded Workflow Iteration](0020-bounded-workflow-iteration.md)
