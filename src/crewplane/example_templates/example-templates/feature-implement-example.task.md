---
schema_version: "__SCHEMA_VERSION__"
name: Feature Implementation Example
description: Use an input brief to plan, implement, review, and hand off a feature.
inputs:
  feature_brief: feature.brief
nodes:
  - id: feature.brief
    mode: input
    source: "{{file:.crewplane/workflows/example-templates/sample-inputs/feature-brief.md}}"
  - id: implement.plan
    mode: parallel
    needs: [feature.brief]
    providers: [claude]
  - id: implement.build
    mode: parallel
    needs: [implement.plan]
    providers: [codex]
  - id: implement.iterate
    mode: sequential
    needs: [implement.build]
    review_starts_with: reviewer
    depth: 2
    audit_rounds: 1
    continue_on_failure: true
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
      - provider: gemini
        role: reviewer
  - id: implement.handoff
    mode: parallel
    needs: [implement.iterate]
    providers: [claude]
---

## implement.plan

Open the complete feature request at `{{feature.brief.output_path}}`.
Inspect only the project files needed to choose the implementation and tests.
Create a concrete implementation plan without changing files.

Return scope, touched components, implementation steps, and validation strategy.

## implement.build

Open the complete plan at `{{implement.plan.output_path}}` and the acceptance
criteria at `{{feature.brief.output_path}}`. Implement the agreed scope and run
the relevant checks. Report any check you could not run.

Return changed files, key implementation decisions, and commands run.

For files created or changed during this invocation, include a link-only section
with one actual project-relative path per line:

## Generated Files
- path/to/file.ext

## implement.iterate

Open the original implementation report at `{{implement.build.output_path}}`
and the acceptance criteria at `{{feature.brief.output_path}}`. Use those files
for the initial review, then assess this node's current candidate and its code.

<!-- crewplane:executor -->
Apply required fixes or explain why no fix is needed. Return the complete
current implementation report, commands and results, and remaining concerns.
Include an exact `## Generated Files` section with one project-relative path or
link per line for files created or changed during this invocation.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Check the implementation against the acceptance criteria, with attention to
correctness, regressions, and missing validation. Cite concrete evidence for
any requested changes.
<!-- /crewplane:reviewer -->

## implement.handoff

Open the plan at `{{implement.plan.output_path}}`, the original implementation
report at `{{implement.build.output_path}}`, and the final reviewed result at
`{{implement.iterate.output_path}}`. Create a final handoff without changing files.

Include rollout notes, validation commands and results, captured file links,
reviewer approval or unresolved objections, and follow-up tasks. A completed run
does not itself prove approval when the config allows review exhaustion.
