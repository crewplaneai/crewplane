---
schema_version: "__SCHEMA_VERSION__"
name: Code Review Example
description: Parallel review context, iterative review rounds, compact findings, and a final readiness report.
nodes:
  - id: review.context
    mode: parallel
    findings: true
    providers: [codex, claude, gemini]
    failure_threshold: 1
    continue_on_failure: true
  - id: review.iterate
    mode: sequential
    needs: [review.context]
    audit_rounds: 2
    depth: 2
    continue_on_failure: true
    token_budget:
      warn_threshold_chars: 30000
      fail_threshold_chars: 90000
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
      - provider: gemini
        role: reviewer
  - id: review.summary
    mode: sequential
    needs: [review.iterate]
    token_budget:
      warn_threshold_chars: 20000
      fail_threshold_chars: 60000
    providers: [claude]
---

## review.context

Review `{{var:project_name}}` for public-release readiness.

Use the generated configuration as the provider and runtime contract:
{{file:.orchestrator/config.yml}}

Return:
1. top correctness and regression risks
2. missing validation or release-blocking gaps
3. concrete questions the review loop should resolve

End with exactly one concise findings block:
<!-- findings -->
- finding with file, behavior, and recommended next action
<!-- /findings -->

## review.iterate

Run iterative review rounds from this compact findings artifact:
{{review.context.findings}}

Findings artifact metadata:
- Path: {{review.context.findings_path}}
- Size: {{review.context.findings_size}}
- SHA-256: {{review.context.findings_sha256}}

<!-- orchestrator:executor -->
Produce the current release-readiness candidate.
Resolve or disposition each finding, cite the evidence used, and include the
validation you would run before committing.
<!-- /orchestrator:executor -->

<!-- orchestrator:reviewer -->
Review only the current candidate for correctness, regressions, missing
validation, and unsupported claims. Do not edit files.

End with this structure:

## Major Issues
None

## Minor Issues
None

## Nitpicks
None

---
VERDICT: NO_FINDINGS

Use `CHANGES_REQUESTED` when major or minor issues remain. Use `NITS_ONLY` only
for optional polish.
<!-- /orchestrator:reviewer -->

## review.summary

Create one concise final report for the run.

Use:
{{review.context.findings}}

The review-loop output is available at:
- Path: {{review.iterate.output_path}}
- Size: {{review.iterate.output_size}}
- SHA-256: {{review.iterate.output_sha256}}

Include:
1. Severity-ranked findings
2. Review consensus status
3. Recommended fixes or explicit approval rationale
4. Merge readiness verdict
