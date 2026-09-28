---
schema_version: "__SCHEMA_VERSION__"
name: Test Generation Example
description: Scope, generate, review, remediate, and summarize tests for a change.
nodes:
  - id: tests.scope
    mode: parallel
    providers: [claude]
  - id: tests.generate
    mode: parallel
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
    mode: parallel
    needs: [tests.fixes]
    providers: [claude]
---

## tests.scope

Define test scope for `{{var:project_name}}`.

Use this feature brief and coding standard:
{{file:.crewplane/workflows/example-templates/sample-inputs/feature-brief.md}}
{{file:.crewplane/workflows/example-templates/sample-inputs/coding-standards.md}}

Inspect the implementation named by the brief and identify the existing test
tools. Do not change files. If the target implementation does not exist, report
that gap instead of inventing an API. Return its paths, behavior to verify, and
specific test cases. Focus on high-risk paths and deterministic local tests.

## tests.generate

Open the complete test scope at `{{tests.scope.output_path}}`. Add the planned
tests using the project's existing tools, without changing production behavior
to make tests pass. If the required implementation is missing, explain the
blocker rather than inventing it. Run the relevant tests and report the commands,
results, and any checks that could not run.

Include an exact `## Generated Files` section with one project-relative path or
link per line for files created or changed during this invocation.

## tests.review

Open the test report at `{{tests.generate.output_path}}` and the scope at
`{{tests.scope.output_path}}`. Inspect the named tests and implementation for
missing assertions, flaky patterns, and untested edge cases. Do not change files.

End with exactly one findings block:
<!-- findings -->
- concrete test gap or review finding
<!-- /findings -->

## tests.fixes

Use these findings and open the test scope at `{{tests.scope.output_path}}`:
{{tests.review.findings}}

<!-- crewplane:executor -->
Fix confirmed test issues without changing production behavior. Run the focused
tests and return the complete current test report, commands, results, and any
remaining gaps. Do not claim passing tests when a check was not run.
Include an exact `## Generated Files` section with one project-relative path or
link per line for files created or changed during this invocation.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Check that the tests cover the intended behavior and that the fixes address
reported gaps without hiding failures or adding flaky assertions.
<!-- /crewplane:reviewer -->

## tests.summary

Open the original test report at `{{tests.generate.output_path}}`, the findings
at `{{tests.review.findings_path}}`, and the reviewed fixes at
`{{tests.fixes.output_path}}`. Summarize test coverage, actual check results,
captured file links, reviewer approval or unresolved objections, and next actions.
Do not change files or equate workflow completion with passing tests.
