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

<!-- orchestrator:reviewer -->
Review the executor output for correctness and regressions.
<!-- /orchestrator:reviewer -->
```

Unmarked Markdown is shared prompt content. Authored role markers are only
`executor` and `reviewer`; there is no authored `shared` marker.

## Controls

- `audit_rounds`: maximum review-loop rounds for a sequential node with
  reviewers. It must not exceed `settings.max_audit_rounds`.
- `depth`: sequential execution depth; must be positive when supplied.
- `continue_on_failure`: allow downstream scheduling to continue past this node
  failure when workflow policy permits.
- `failure_threshold`: parallel-node failure threshold. It is not valid on
  sequential nodes.

Review-loop status is persisted under each node stage directory, and final
results are written from the runtime-owned review-loop status artifact.
