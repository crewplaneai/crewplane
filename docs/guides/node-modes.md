# Node Modes And Provider Roles

Workflow node `mode` controls how Crewplane invokes providers inside that node.
Use `needs` to order nodes in the DAG; use `mode` to choose the execution
pattern for one node.

## Choosing A Mode

Most users start with `parallel`. It is the simplest provider invocation shape,
even when there is only one provider.

| Mode | Use when | Provider shape |
| --- | --- | --- |
| `input` | A workflow needs to expose a file as an artifact without invoking a provider. | No providers. |
| `parallel` | Several providers should answer the same prompt independently. | One or more executor providers. |
| `sequential` | One provider should run in order, or executor output should be reviewed and remediated. | One executor, or executor segment followed by reviewer segment. |

Independent DAG nodes can also run concurrently when their dependencies are
satisfied. That is separate from `mode: parallel`, which is provider fanout
inside one node.

## Run One Provider

Use `parallel` with one provider for the common single-agent case:

```yaml
nodes:
  - id: review.project
    mode: parallel
    providers: [codex]
```

This renders one prompt, invokes one executor provider, and writes one node
result.

## Fan Out To Multiple Providers

`mode: parallel` renders the node prompt once and sends the same executor prompt
to every provider at the same time:

```yaml
nodes:
  - id: compare.designs
    mode: parallel
    providers:
      - codex
      - claude
      - gemini
```

Each provider writes its own stage artifact. Finalization aggregates the latest
output for each provider task into the node result, so downstream
`{{compare.designs.output}}` contains one section per selected provider output.

Parallel mode rules:

- Providers are executors. `role: reviewer` is not valid.
- `depth` and `audit_rounds` are not valid.
- `failure_threshold` is valid only for parallel nodes.
- `settings.max_parallel_invocations` can cap provider calls inside the node.

Useful cases:

- Compare multiple models on the same question.
- Generate several independent proposals before a later synthesis node.
- Run redundant providers so one timeout or quota failure does not block useful
  output.
- Ask independent agents to inspect the same artifact, then feed the combined
  result to a downstream node.

Failure controls:

```yaml
nodes:
  - id: inspect
    mode: parallel
    providers: [codex, claude, gemini]
    failure_threshold: 1
    continue_on_failure: true
```

By default, no failures are allowed. `failure_threshold: 1` allows one provider
failure. If failures exceed the threshold, `continue_on_failure: true` lets the
node complete and preserves synthetic failure artifacts in the stage output.

Parallel mode is not a review loop. If you need an executor followed by one or
more reviewers, use `mode: sequential`.

## Sequential Executor Rounds

A sequential node with one provider is a single executor path:

```yaml
nodes:
  - id: implement
    mode: sequential
    providers: [codex]
    depth: 2
```

The provider must be an executor. `depth` is the total number of executor rounds,
and finalization selects the latest round for that task. `audit_rounds` is not
valid because there are no reviewers.

Use this shape when the node needs ordered executor retries or when later rounds
should operate on the previous candidate workspace state. It does not add review
feedback; for review feedback, use a multi-provider sequential node.

## Executor + Reviewer Loop

A sequential node with multiple providers becomes a review loop. Providers must
be declared as a contiguous executor segment followed by a contiguous reviewer
segment:

```yaml
nodes:
  - id: implement
    mode: sequential
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
    review_starts_with: executor
    depth: 1
    audit_rounds: 1
```

The runtime splits the provider list into executors and reviewers. Each local
round runs the executor candidate, sends the current candidate to reviewers, and
checks for consensus.

With one reviewer, consensus means that reviewer approved. With multiple
reviewers, reviewers run in parallel and every reviewer must approve.

For task-oriented review-loop guidance, see
[How to Produce Findings and Run Review Loops](findings-and-review-loops.md).

## Detailed Review-Loop Semantics

| Reviewer count | Runtime behavior | Consensus rule |
| --- | --- | --- |
| One reviewer | One reviewer receives the reviewer prompt and current executor output. | That reviewer must approve. |
| Multiple reviewers | All reviewers receive the same reviewer prompt and current executor output in parallel. | Every reviewer must approve. |

Approving verdicts are `NO_FINDINGS` and `NITS_ONLY`. `CHANGES_REQUESTED`,
unstructured feedback, ambiguous output, or a reviewer invocation failure does
not approve the candidate. A blocking review drives remediation; an invocation
failure aborts unless `continue_on_failure: true` allows continuation.

Review-loop controls:

- `depth`: remediation attempts inside each audit round. `depth: 1` means the
  initial candidate can receive one fix attempt after reviewer feedback.
- `audit_rounds`: fresh audit passes. A clean first-round approval stops the
  loop. If consensus is reached only after remediation and more audit rounds are
  allowed, the next audit round can re-check the canonical candidate.
- `review_starts_with`: first phase inside the review loop. `executor` is the
  default. `reviewer` adds a round-0 reviewer pass against existing review
  context before the local-round-1 executor candidate.
- `settings.max_audit_rounds`: configuration limit for `audit_rounds`.
- `settings.max_parallel_invocations`: optional cap for parallel reviewer calls.

Execution order is nested: `audit_rounds` is the outer loop, and `depth` controls
the inner remediation loop. Each audit round can run up to `depth + 1` local
review passes because local round 1 reviews the initial candidate, then each
remaining local round is a fix attempt.

For `depth: 2` and `audit_rounds: 3`, the maximum order is:

```text
Audit round 1
  Local round 1: run executor candidate, then run reviewer(s)
  Local round 2: if blocked, run executor remediation, then reviewer(s)
  Local round 3: if blocked, run executor remediation, then reviewer(s)

Audit round 2
  Local round 1: re-review latest valid executor output from audit round 1
  Local round 2: if blocked, run executor remediation, then reviewer(s)
  Local round 3: if blocked, run executor remediation, then reviewer(s)

Audit round 3
  Local round 1: re-review latest valid executor output from audit round 2
  Local round 2: if blocked, run executor remediation, then reviewer(s)
  Local round 3: if blocked, run executor remediation, then reviewer(s)
```

A clean approval in local round 1 stops the whole loop immediately. Consensus
after remediation stops the current audit round; if more audit rounds remain,
the next audit round re-checks the latest valid output as local round 1. If no
valid output exists from a prior audit round, the next audit round invokes the
executor for local round 1.

Reviewer-first order is valid only for the same sequential review-loop provider
shape. Providers still declare executors first and reviewers second:

```yaml
nodes:
  - id: review.fix
    mode: sequential
    needs: [implement.backend, implement.frontend]
    review_starts_with: reviewer
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
      - provider: gemini
        role: reviewer
```

```markdown
## review.fix
Review these inputs and fix only the issues that need changes:

- backend output: {{implement.backend.output}}
- frontend output: {{implement.frontend.output}}
- backend digest: {{implement.backend.output_sha256}}
```

The round-0 reviewers run in parallel and must unanimously approve. If they
approve, the executor still runs local round 1 with preservation guidance so
the node produces a canonical executor output. If they request changes, their
major and minor feedback becomes the executor handoff. `depth: 1` keeps its
usual meaning: one remediation attempt after the local-round-1 candidate.

`needs` orders the DAG but does not automatically define review context. Put
the artifacts, findings, metadata, or files reviewers should inspect directly
in shared or reviewer prompt text. Standalone project-root reviewer-first nodes
can review visible files with `{{file:...}}`. With Experimental managed
workspaces, reviewer-first `{{file:...}}` references resolve from the compiled
Git source selected by workspace policy: same-node candidate when one exists,
then upstream lineage, then project initial source.

## Prompt Roles

Unmarked Markdown is shared prompt content. Role markers add role-specific
content:

```markdown
## implement
Build the requested change.

<!-- crewplane:executor -->
Focus on the implementation and write the final artifact.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Review for correctness, regressions, and missing tests.
<!-- /crewplane:reviewer -->
```

Executors receive shared content plus executor segments. Reviewers receive
shared content plus reviewer segments. During review loops, Crewplane also adds
reviewer-only safety instructions, the current executor output, any unresolved
feedback from the previous round, and the structured review contract.
For `review_starts_with: reviewer`, the round-0 reviewer prompt labels the
existing review context instead of claiming a current same-node executor output
exists.

Multiple reviewers receive the same reviewer prompt. They do not see each
other's current-round feedback before responding. A structured review block is
the reliable contract; clear plain-language approval or blocker cues may be
inferred, but ambiguous output does not approve the candidate.

## Workspace Notes

When Experimental worktrees are enabled, reviewer invocations inspect the
current executor candidate but do not advance source lineage. Executor and
remediation rounds produce candidate lineage. A mutable `kind: worktree` node
can have only one executor provider; reviewer providers remain allowed in
sequential review loops.

See also:

- [Findings and review loops](findings-and-review-loops.md)
- [Workflow syntax reference](../reference/workflow-syntax.md)
- [Experimental workspace isolation](workspace-isolation.md)
