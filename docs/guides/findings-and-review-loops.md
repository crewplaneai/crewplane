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

## Controls

- `audit_rounds`: maximum review-loop audit rounds for a sequential node with
  reviewers. It defaults to `1`, is only valid when the sequential node has
  multiple providers, and must not exceed `settings.max_audit_rounds`.
- `depth`: sequential execution depth. It defaults to `1` and must be positive
  when supplied. For a single-provider sequential node, it is the total executor
  rounds. For a review loop, it is remediation depth inside each audit round.
- `continue_on_failure`: converts selected parallel or review-loop failure
  outcomes into successful node completion. Failed dependencies still block
  downstream nodes. It applies to parallel failure-threshold excess, reviewer
  failures, and review-loop consensus exhaustion.
- `failure_threshold`: parallel-node failure threshold. It is not valid on
  sequential nodes.

Review-loop status is persisted under each node stage directory, and final
results are written from the runtime-owned review-loop status artifact.
