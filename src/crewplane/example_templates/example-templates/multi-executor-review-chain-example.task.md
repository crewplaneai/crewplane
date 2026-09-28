---
schema_version: "__SCHEMA_VERSION__"
name: Multi Executor Review Example
description: Run two proposal passes in order, then review their complete outputs together.
nodes:
  - id: chain.context
    mode: parallel
    providers: [claude]
  - id: chain.iterate
    mode: sequential
    needs: [chain.context]
    audit_rounds: 2
    depth: 2
    continue_on_failure: true
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: executor
      - provider: gemini
        role: reviewer
  - id: chain.summary
    mode: parallel
    needs: [chain.iterate]
    providers: [claude]
---

## chain.context

Prepare a short change brief for `{{var:project_name}}`.

Use this feature brief:
{{file:.crewplane/workflows/example-templates/sample-inputs/feature-brief.md}}

Return:
1. Goal
2. Constraints
3. Files likely to change

Preserve the request's acceptance criteria. Inspect relevant files but do not
change them.

## chain.iterate

Open the change brief at `{{chain.context.output_path}}`.

Both executors receive the same instructions and produce proposals for review
together. They run in order, but one executor's current response is not passed
to the next executor. Keep project files unchanged throughout this node.

<!-- crewplane:executor -->
Write a self-contained implementation proposal covering the requested behavior,
files to change, tradeoffs, risks, and validation. On a fix attempt, address the
review feedback and return your complete revised proposal. Do not implement it
or assume another executor's response is available.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Evaluate both proposals against the change brief. Check feasibility, acceptance
criteria, regressions, and validation. Cite which proposal each issue affects;
identify conflicting assumptions that need resolution before implementation.
<!-- /crewplane:reviewer -->

## chain.summary

Open the change brief at `{{chain.context.output_path}}` and the complete proposal
and review results at `{{chain.iterate.output_path}}`. Do not change files.

Include:
1. Where the proposals agree and differ
2. A recommended approach and its rationale, preserving relevant constraints
3. Reviewer approval or unresolved objections, and follow-up work

The workflow does not merge proposals or select a winner automatically. Do not
claim implementation happened or infer approval from workflow completion.
