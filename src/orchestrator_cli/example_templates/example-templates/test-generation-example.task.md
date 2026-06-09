---
schema_version: "__SCHEMA_VERSION__"
name: Test Generation Example
description: Scope, generate, review, remediate, and summarize tests for a change.
nodes:
  - id: tests.scope
    mode: sequential
    providers: [claude]
  - id: tests.generate
    mode: sequential
    needs: [tests.scope]
    providers: [codex]
  - id: tests.review
    mode: parallel
    findings: true
    needs: [tests.generate]
    providers: [claude, gemini]
    failure_threshold: 1
    continue_on_failure: true
  - id: tests.fixes
    mode: sequential
    needs: [tests.review]
    depth: 3
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
  - id: tests.summary
    mode: sequential
    needs: [tests.fixes]
    providers: [claude]
---

## tests.scope

Define test scope for `{{var:project_name}}`.

Use this feature brief and coding standard:
{{file:.orchestrator/workflows/example-templates/sample-inputs/feature-brief.md}}
{{file:.orchestrator/workflows/example-templates/sample-inputs/coding-standards.md}}

Focus on high-risk paths, edge cases, and deterministic filesystem-local tests.

## tests.generate

Generate tests using this scope:
{{tests.scope.output}}

Include unit tests and integration coverage where appropriate.

## tests.review

Review generated tests using:

Generated test artifact:
- Path: {{tests.generate.output_path}}
- Size: {{tests.generate.output_size}}
- SHA-256: {{tests.generate.output_sha256}}

Check for missing assertions, flaky patterns, and untested edge cases.

End with exactly one findings block:
<!-- findings -->
- concrete test gap or review finding
<!-- /findings -->

## tests.fixes

Apply fixes based on review output:
{{tests.review.findings}}

Ensure all tests pass and assertions are comprehensive.
Reviewer approval should happen only when no major or minor issues remain; optional nitpicks may remain.

## tests.summary

Summarize the final test plan from:
{{tests.generate.output}}
{{tests.fixes.output}}

Review findings metadata:
- Path: {{tests.review.findings_path}}
- Size: {{tests.review.findings_size}}
- SHA-256: {{tests.review.findings_sha256}}

Include remaining gaps and next actions.
