---
schema_version: "__SCHEMA_VERSION__"
name: Bug Fix Example
description: Reproduce a reported failure, diagnose its cause, and review a focused fix with regression coverage.
# Use run --force to inspect current project files again when the report is unchanged.
inputs:
  bug_report: bug.report
nodes:
  - id: bug.report
    mode: input
    source: "{{file:.crewplane/workflows/example-templates/sample-inputs/bug-report.md}}"
  - id: bug.reproduce
    mode: parallel
    needs: [bug.report]
    providers: [claude]
  - id: bug.diagnose
    mode: parallel
    needs: [bug.reproduce]
    providers: [codex]
  - id: bug.fix
    mode: sequential
    needs: [bug.diagnose]
    depth: 2
    audit_rounds: 1
    token_budget:
      warn_threshold_chars: 25000
      fail_threshold_chars: 75000
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
  - id: bug.verify
    mode: parallel
    needs: [bug.fix]
    providers: [claude]
---

## bug.reproduce

Open the complete bug report at `{{bug.report.output_path}}`. Inspect the named
implementation and its tests, then run a focused reproduction using the
project's existing tools. Keep source and test files unchanged; normal temporary
test outputs are allowed. Preserve any existing user edits.

Record the exact input, command, observed output, expected output, relevant
file paths, and environment details needed to repeat the check. Distinguish a
reproduced defect from a passing check or a check blocked by missing code,
dependencies, or access. The sample report is hypothetical: do not manufacture
a failure or implement a missing feature to satisfy it.

## bug.diagnose

Open the report at `{{bug.report.output_path}}` and reproduction evidence at
`{{bug.reproduce.output_path}}`. Trace a reproduced failure through the named
code without changing files. Inspect only the additional files needed to test
the causal explanation.

Return the supported root cause with file references, rejected explanations,
the smallest proposed correction, and a regression test that distinguishes the
observed failure from the required behavior. Preserve the reproduction command
and expected result. If no defect was reproduced, explain whether the claim was
not observed or investigation was blocked; do not invent a diagnosis or patch.

## bug.fix

Open the original report at `{{bug.report.output_path}}`, the reproduction at
`{{bug.reproduce.output_path}}`, and the diagnosis at
`{{bug.diagnose.output_path}}`. Keep the fix within the reported behavior and
preserve unrelated changes.

<!-- crewplane:executor -->
For a reproduced and supported defect, add the focused regression test and
confirm that it exposes the original failure before applying the correction.
Then implement the smallest fix and rerun that test and the relevant existing
checks. On later attempts, address review feedback without undoing an earlier
fix just to reproduce the original failure again.

If there is no supported defect, leave source and tests unchanged and explain
why no fix was made. Record missing validation as a limitation, not a passing
check. Do not reset, discard, stage, or commit user changes.

Return the complete current fix report: causal explanation, cumulative changed
files, regression evidence before and after the fix, commands and results, and
remaining concerns. If files were created or changed during this invocation,
end with an exact `## Generated Files` section containing one project-relative
path or Markdown link per line. List only this invocation's changes there;
report deleted files separately. Omit the section if no files changed.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Check that the correction addresses the demonstrated cause and that the
regression test distinguishes the original and corrected behavior. Look for
scope expansion, regressions, and unsupported claims about completed checks.
For a no-change result, assess whether the evidence supports that outcome and
whether any missing prerequisites leave the report unresolved.
<!-- /crewplane:reviewer -->

## bug.verify

Open the original report at `{{bug.report.output_path}}`, the reproduction at
`{{bug.reproduce.output_path}}`, and the complete reviewed result at
`{{bug.fix.output_path}}`. Independently rerun the original reproduction and
focused regression checks against the current files when possible. Keep source
and tests unchanged; normal temporary test outputs are allowed.

Return the final verification report with observed results, changed-file and
captured-file references, reviewer approval or unresolved objections, and any
follow-up work. Distinguish a verified fix, a report that was not reproduced,
and blocked verification. Identify checks you ran separately from earlier
reported results. Workflow completion alone does not establish reviewer
approval or passing tests, and this final report does not trigger another fix
cycle automatically.
