# Findings And Review Loops

Crewplane supports structured findings artifacts and sequential executor/reviewer
loops.

## Findings

Set `findings: true` on a provider node when downstream nodes need a separate
findings artifact:

```yaml
nodes:
  - id: review.context
    mode: parallel
    providers: ["claude", "codex"]
    findings: true
```

Downstream nodes can reference:

- `{{review.context.output}}`
- `{{review.context.findings}}`
- `{{review.context.output_path}}`
- `{{review.context.findings_path}}`

`{{node.findings}}` and `{{node.findings_*}}` references are valid only when the
upstream node declares `findings: true`.

## Sequential Review Loops

A sequential node with multiple providers must declare a contiguous executor
segment followed by a contiguous reviewer segment:

```yaml
nodes:
  - id: implement
    mode: sequential
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
    audit_rounds: 2
    depth: 1
```

This is the only node shape that runs a review loop. A sequential node with one
provider is a plain executor node, and a parallel node never accepts reviewers.

Reviewer prompts must be present when reviewer providers are configured:

```markdown
## implement
Implement the requested change.

<!-- crewplane:reviewer -->
Review the executor output for correctness and regressions.
<!-- /crewplane:reviewer -->
```

Unmarked Markdown is shared prompt content. Authored role markers are only
`executor` and `reviewer`; there is no authored `shared` marker.

Executors receive shared content plus executor segments. Reviewers receive
shared content plus reviewer segments. During a review loop, Crewplane adds the
current executor output, any previous unresolved feedback, reviewer-only safety
instructions, and the review contract to the reviewer prompt.

## Reviewer Counts

One reviewer and multiple reviewers use the same review-loop implementation.
The difference is the review phase:

| Reviewer count | Runtime behavior | Consensus rule |
| --- | --- | --- |
| One reviewer | One reviewer receives the reviewer prompt and current executor output. | That reviewer must approve. |
| Multiple reviewers | Reviewers run in parallel with the same reviewer prompt and current executor output. | Every reviewer must approve. |

Reviewers do not see each other's current-round feedback before responding.
`settings.max_parallel_invocations` can cap parallel reviewer calls.
When consensus fails, Crewplane carries unresolved major issues, minor issues,
and unstructured feedback into the next executor remediation prompt. Nitpicks
are not carried forward unless the executor chooses to address them.

Reviewers are instructed to end with a normalized review block:

```markdown
## Major Issues
None

## Minor Issues
None

## Nitpicks
None

---
VERDICT: CHANGES_REQUESTED | NITS_ONLY | NO_FINDINGS
```

`NO_FINDINGS` and `NITS_ONLY` approve a candidate. `CHANGES_REQUESTED` blocks
it. All reviewers must approve for consensus. Malformed or ambiguous reviewer
output is preserved as unstructured feedback and is not treated as approval.
Plain-language approval or blocker cues may be inferred when no structured
block is present, but the structured block is the reliable contract.

## Loop Flow

Review-loop execution is nested. `audit_rounds` is the outer loop, and each
audit round runs a bounded local loop with up to `depth + 1` local review
passes. Local round 1 reviews the initial candidate. Each later local round is a
remediation attempt after reviewer feedback.

Inside one audit round:

1. Executor providers produce a candidate.
2. Crewplane validates that a usable candidate exists.
3. Reviewer providers inspect the current candidate.
4. If all reviewers approve, the candidate reaches consensus.
5. If consensus fails, unresolved reviewer feedback is written to review-state
   artifacts and injected into the next remediation prompt.
6. Remediation repeats up to `depth` fix attempts for that audit round.

For example, `depth: 2` and `audit_rounds: 3` can run in this order:

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

If a clean candidate is approved in the first local round, the review loop
finishes immediately. If consensus is reached after remediation and
`audit_rounds` allows more fresh audit passes, Crewplane can run another audit
round against the latest valid candidate. If no valid candidate exists from a
prior audit round, the next audit round invokes the executor for local round 1.

## Controls

- `audit_rounds`: maximum review-loop audit rounds for a sequential node with
  reviewers. It defaults to `1`, is only valid when the sequential node has
  multiple providers, and must not exceed `settings.max_audit_rounds`.
- `depth`: sequential execution depth. It defaults to `1` and must be positive
  when supplied. For a single-provider sequential node, it is the total executor
  rounds. For a review loop, it is remediation depth inside each audit round;
  `depth: 1` means one fix attempt after the initial reviewed candidate.
- `continue_on_failure`: converts selected parallel or review-loop failure
  outcomes into successful node completion. Failed dependencies still block
  downstream nodes. It applies to parallel failure-threshold excess, reviewer
  invocation failures, and review-loop consensus exhaustion. A reviewer verdict
  of `CHANGES_REQUESTED` is not an invocation failure; it drives remediation.
- `failure_threshold`: parallel-node failure threshold. It is not valid on
  sequential nodes.

Review-loop status is persisted under each node stage directory, and final
results are written from the runtime-owned review-loop status artifact.

For mode selection and parallel-node examples, see
[Node modes and provider roles](node-modes.md).
