# Findings Artifacts

Crewplane can write a separate findings artifact for a provider node. Use this
when downstream nodes need concise structured issues instead of the full node
output.

For executor/reviewer approval loops, see [Review loops](review-loops.md).

## Enable Findings

Set `findings: true` on a provider node:

```yaml
nodes:
  - id: review.context
    mode: parallel
    providers: ["claude", "codex"]
    findings: true
```

When the node completes, Crewplane writes the normal output artifact and a
separate findings artifact under the run result directory:

```text
.crewplane/execution-results/<run-key>/review.context-result.md
.crewplane/execution-results/<run-key>/review.context-findings.md
```

## Multi-Provider Findings

For a parallel node with multiple providers, Crewplane extracts findings from
each selected executor output and writes one findings file with one section per
provider task. It does not merge all bullets into one flat list, and it does not
choose only one provider's findings.

For example, if `claude` and `codex` both produce a valid findings block, the
findings artifact is sectioned like:

```markdown
## claude (executor)

- claude finding

---

## codex (executor)

- codex finding

---
```

Each selected non-empty executor output must contain exactly one findings
block. Reviewer outputs are not selected for findings extraction.

The `crewplane init` mock config exercises this path automatically with
deterministic generated findings. Provider-backed runs require the authored
prompt to ask the provider for exactly one findings block.

## Author Provider Instructions

When the node declares `findings: true`, ask for exactly one findings block:

```markdown
End with exactly one concise findings block:
<!-- findings -->
- finding with affected file, behavior, and recommended next action
<!-- /findings -->
```

Findings should be concise and actionable. Put long rationale, exploration, or
full review notes in the regular node output.

## Reference Findings Downstream

Downstream nodes can reference both the full output and the findings artifact:

- `{{review.context.output}}`
- `{{review.context.findings}}`
- `{{review.context.output_path}}`
- `{{review.context.findings_path}}`
- `{{review.context.output_sha256}}`
- `{{review.context.findings_sha256}}`

`{{node.findings}}` and `{{node.findings_*}}` references are valid only when the
upstream node declares `findings: true`.

Example downstream prompt:

```markdown
## plan.fix
Use the findings from the review node:

{{review.context.findings}}

Write a minimal fix plan and cite the related files.
```

## When To Use Findings Or Output

Use `{{node.findings}}` when a downstream node needs a concise issue handoff:

- a later node should fix or summarize only reported issues
- support bundles need a small issue list
- multiple provider outputs need a stable issue-oriented handoff
- prompt budgets would be strained by injecting full upstream outputs

Use `{{node.output}}` when a downstream node needs the full result:

- downstream nodes need the full implementation or design text
- the provider output is already short
- the node is only collecting context that is not issue-shaped

It is normal to use both in the same workflow. For example, send
`{{review.context.findings}}` to a fix-planning node, and send
`{{review.context.output}}` to a later node that needs the full review notes or
original design.

## Troubleshooting

If a downstream `{{node.findings}}` reference fails, check that the upstream node
declares `findings: true`.

If a findings file is missing or finalization fails after a provider-backed run,
inspect the selected executor output files and provider logs. Findings
extraction expects exactly one usable findings block from each selected
non-empty executor output.

## Next

Continue to [Workflow Composition](workflow-composition.md) to split reusable
workflow pieces into imports, aliases, parameters, and inputs.


Or return to the [Guides](../index.md#guides).
