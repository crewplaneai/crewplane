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
    mode: sequential
    needs: [feature.brief]
    providers: [claude]
  - id: implement.build
    mode: sequential
    needs: [implement.plan]
    token_budget:
      warn_threshold_chars: 25000
      fail_threshold_chars: 75000
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
    mode: sequential
    needs: [implement.iterate]
    providers: [claude]
---

## implement.plan

Create a concrete implementation plan for this feature request:
{{feature.brief.output}}

Return scope, touched components, implementation steps, and validation strategy.

## implement.build

Implement the feature based on this plan:
{{implement.plan.output}}

Return changed files, key implementation decisions, and commands run.

If files are created or changed, include a link-only section:

## Generated Files
- path/to/file.ext

## implement.iterate

Review and remediate the implementation candidate.

Implementation artifact:
{{implement.build.output}}

Implementation metadata:
- Path: {{implement.build.output_path}}
- Size: {{implement.build.output_size}}
- SHA-256: {{implement.build.output_sha256}}

<!-- crewplane:executor -->
Apply required fixes or explain why no fix is needed. Return the complete
current candidate, commands run, and a link-only `Generated Files` section when
the workspace changes.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Review the current candidate for correctness, regressions, and missing
validation. Do not edit files.

End with `Major Issues`, `Minor Issues`, `Nitpicks`, and a final
`VERDICT: CHANGES_REQUESTED`, `VERDICT: NITS_ONLY`, or
`VERDICT: NO_FINDINGS`.
<!-- /crewplane:reviewer -->

## implement.handoff

Create a final handoff summary from:
{{implement.plan.output}}
{{implement.iterate.output}}

Include rollout notes, validation commands, generated-file paths, and follow-up
tasks.
