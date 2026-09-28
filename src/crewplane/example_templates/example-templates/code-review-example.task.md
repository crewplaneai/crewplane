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

Read the project's README, contribution instructions, and relevant source and
tests. Do not change files. Report which areas you inspected and which checks
you could not complete.

Return:
1. top correctness and regression risks
2. missing validation or release-blocking gaps
3. concrete questions the review loop should resolve

End with exactly one concise findings block:
<!-- findings -->
- finding with file, behavior, and recommended next action
<!-- /findings -->

## review.iterate

Check and refine the review report using these findings:
{{review.context.findings}}

<!-- crewplane:executor -->
Verify the reported issues against the named files. Return a complete review
report with confirmed issues, rejected claims, evidence, and recommended fixes.
Do not implement fixes or change files. Treat failed provider calls as missing
review coverage, not approval.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Check that each reported issue has concrete evidence and that the proposed fixes
address it. Flag unsupported conclusions and missing review coverage.
<!-- /crewplane:reviewer -->

## review.summary

Create one concise final report for the run.

Open the original findings at `{{review.context.findings_path}}` and the complete
review-loop result at `{{review.iterate.output_path}}`. Do not change files.

Include:
1. Severity-ranked findings
2. Review consensus status
3. Recommended fixes or explicit approval rationale
4. Merge readiness verdict

This workflow allows continuation when a provider fails or review attempts run
out. Distinguish reviewer approval, unresolved objections, and unavailable checks.
Do not treat a completed workflow as proof that the project is ready to merge.
