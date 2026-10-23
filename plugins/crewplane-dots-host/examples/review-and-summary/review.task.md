---
schema_version: "1.0"
name: Dots host review and summary
nodes:
  - id: review
    mode: parallel
    providers: [dots]
  - id: summarize
    mode: parallel
    providers: [dots]
    needs: [review]
---

## review

Read-only code review of the checked-out Crewplane codebase, with particular attention to the new plugins/crewplane-dots-host integration and its tests, plus the existing generic CLI adapter/runtime boundaries that it uses. The checkout root is four directories above this example directory. Read AGENTS.md there. Inspect actual source and run bounded local tests if useful. Do not modify source, commit, push, use paid APIs, or contact anyone. Do not read unrelated files. Report concrete correctness, safety, lifecycle and compatibility issues with file/line references, severity and suggested fixes. Explicitly state scope, checks run, and limitations. Do not claim exhaustive review. The baseline commit is 762143ada19642965f4670e76faa1d5960e10e73. Return the review as your final result so Crewplane records it as the first-stage artifact.

## summarize

Summarize the ACTUAL upstream review below, not a newly invented review. This is read-only. Group material findings by severity, explain what works, outstanding limits, and whether the bridge is ready for an active-host demonstration versus unattended deployment. Preserve caveats and do not silently claim fixes were made. Use plain language and be concise.

Upstream review artifact:
{{review.output}}
