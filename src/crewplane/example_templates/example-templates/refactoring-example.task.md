---
schema_version: "__SCHEMA_VERSION__"
name: Refactoring Example
description: Audit, plan, execute, review, and hand off maintainability improvements.
nodes:
  - id: refactor.audit
    mode: sequential
    findings: true
    providers: [gemini]
  - id: refactor.plan
    mode: sequential
    needs: [refactor.audit]
    providers: [claude]
  - id: refactor.execute
    mode: sequential
    needs: [refactor.plan]
    depth: 2
    token_budget:
      warn_threshold_chars: 25000
      fail_threshold_chars: 70000
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
    mode: sequential
    needs: [refactor.fixes]
    providers: [claude]
---

## refactor.audit

Audit current code for refactoring opportunities and risk areas.
Use these standards as the refactoring guardrail:
{{file:.crewplane/workflows/example-templates/sample-inputs/coding-standards.md}}

End with exactly one findings block:
<!-- findings -->
- concrete refactoring opportunity with risk and validation note
<!-- /findings -->

## refactor.plan

Create a refactoring plan using:
{{refactor.audit.findings}}

Prioritize low-risk, high-value slices.

## refactor.execute

Execute the refactoring slices from:
{{refactor.plan.output}}

Return changed files and behavior-impact notes.

## refactor.review

Review refactoring output for regressions and maintainability gains:

Refactoring result artifact:
- Path: {{refactor.execute.output_path}}
- Size: {{refactor.execute.output_size}}
- SHA-256: {{refactor.execute.output_sha256}}

Call out rollback points if risk remains.

## refactor.fixes

Apply fixes based on review output:
{{refactor.review.output}}

Confirm if tests pass and risk is mitigated.
Reviewer approval should happen only when no major or minor issues remain; optional nitpicks may remain.

## refactor.handoff

Prepare a final refactoring handoff summary from:
{{refactor.plan.output}}
{{refactor.fixes.output}}

Include validation checklist and remaining debt.
