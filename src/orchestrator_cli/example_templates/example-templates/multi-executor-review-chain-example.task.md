---
schema_version: "__WORKFLOW_SCHEMA_VERSION__"
name: Multi Executor Review Chain Example
description: Show how multiple executors can hand off work before a reviewer cycle.
nodes:
  - id: chain.context
    mode: sequential
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
    mode: sequential
    needs: [chain.iterate]
    providers: [claude]
---

## chain.context

Prepare a short change brief for `{{var:project_name}}`.

Use this feature brief:
{{file:.orchestrator/workflows/example-templates/sample-inputs/feature-brief.md}}

Return:
1. Goal
2. Constraints
3. Files likely to change

## chain.iterate

Work through this handoff chain using the context below:
{{chain.context.output}}

<!-- orchestrator:executor -->
Executor semantics:
- The first executor should make an initial implementation pass.
- The second executor should inspect the same workspace, refine or complete the result,
  and explain any disagreements with the first pass.
Return the full revised candidate, commands run, and generated-file paths when
files change.
<!-- /orchestrator:executor -->

<!-- orchestrator:reviewer -->
Reviewer semantics:
- Evaluate the latest workspace state plus both executor artifacts for correctness,
  regressions, and missing validation.
- Do not edit files.

End with the structured review sections and a final verdict token.
<!-- /orchestrator:reviewer -->

Stop only when no major or minor issues remain; optional nitpicks may remain.

## chain.summary

Summarize the multi-executor review chain from:
{{chain.context.output}}
{{chain.iterate.output}}

Include:
1. What the first executor did
2. What the second executor refined
3. Reviewer verdict and follow-up work
