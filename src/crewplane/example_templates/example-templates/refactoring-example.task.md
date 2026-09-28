---
schema_version: "__SCHEMA_VERSION__"
name: Refactoring Example
description: Audit, plan, execute, review, and hand off maintainability improvements.
nodes:
  - id: refactor.audit
    mode: parallel
    findings: true
    providers: [gemini]
  - id: refactor.plan
    mode: parallel
    needs: [refactor.audit]
    token_budget:
      warn_threshold_chars: 20000
      fail_threshold_chars: 60000
    providers: [claude]
  - id: refactor.execute
    mode: parallel
    needs: [refactor.plan]
    providers: [codex]
  - id: refactor.review
    mode: parallel
    needs: [refactor.execute]
    providers: [claude, gemini]
    failure_threshold: 1
    continue_on_failure: true
  - id: refactor.fixes
    mode: sequential
    needs: [refactor.review]
    depth: 3
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
  - id: refactor.handoff
    mode: parallel
    needs: [refactor.fixes]
    providers: [claude]
---

## refactor.audit

Audit current code for refactoring opportunities and risk areas without changing
files. Keep each recommendation tied to a concrete behavior or maintenance cost.
Use these standards as the refactoring guardrail:
{{file:.crewplane/workflows/example-templates/sample-inputs/coding-standards.md}}

End with exactly one findings block:
<!-- findings -->
- concrete refactoring opportunity with risk and validation note
<!-- /findings -->

## refactor.plan

Create a refactoring plan using:
{{refactor.audit.findings}}

Prioritize low-risk, high-value slices. Return the full plan with affected files,
behavior to preserve, and checks for each slice. Do not change files.

## refactor.execute

Open the complete plan at `{{refactor.plan.output_path}}`. Apply the agreed
slices once, preserving behavior, and run their focused checks. Return changed
files, behavior-impact notes, commands and results, and remaining risks.

Include an exact `## Generated Files` section with one project-relative path or
link per line for files created or changed during this invocation.

## refactor.review

Open the implementation report at `{{refactor.execute.output_path}}` and the
plan at `{{refactor.plan.output_path}}`. Inspect the named changes for regressions
and whether they achieve the intended maintenance benefit. Do not change files.
Return concrete issues with file references and call out rollback points if risk
remains. Report missing validation separately from confirmed defects.

## refactor.fixes

Open the review report at `{{refactor.review.output_path}}` and the original plan
at `{{refactor.plan.output_path}}`.

<!-- crewplane:executor -->
Fix confirmed issues within the planned scope and run the relevant checks.
Return a complete current implementation report, commands and results, rejected
claims with reasons, and any remaining concerns.
Include an exact `## Generated Files` section with one project-relative path or
link per line for files created or changed during this invocation.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Check behavior preservation, correctness of the fixes, and evidence from tests.
Flag any remaining regressions or scope expansion with concrete file references.
<!-- /crewplane:reviewer -->

## refactor.handoff

Open the plan at `{{refactor.plan.output_path}}`, the original implementation
report at `{{refactor.execute.output_path}}`, and the reviewed fixes at
`{{refactor.fixes.output_path}}`. Prepare a handoff without changing files.

Include the complete changed-file list, captured file links, actual validation
results, reviewer approval or unresolved objections, and remaining debt.
