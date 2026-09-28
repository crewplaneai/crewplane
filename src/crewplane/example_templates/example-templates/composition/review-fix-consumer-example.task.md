---
schema_version: "__SCHEMA_VERSION__"
name: Review Fix Consumer Example
description: Consume raw review findings and standards through input nodes, then implement and summarize fixes.
inputs:
  review_input: review-input
  standards_input: standards-input
nodes:
  - id: review-input
    mode: input
    source: "{{file:.crewplane/workflows/example-templates/sample-inputs/review-findings.md}}"
  - id: standards-input
    mode: input
    source: "{{file:.crewplane/workflows/example-templates/sample-inputs/coding-standards.md}}"
  - id: implement.execute
    mode: parallel
    needs: [review-input, standards-input]
    token_budget:
      warn_threshold_chars: 20000
      fail_threshold_chars: 60000
    providers: [codex]
  - id: implement.summary
    mode: parallel
    needs: [implement.execute]
    providers: [claude]
---

## implement.execute

Verify the supplied review claims against the named files and tests. Apply only
confirmed fixes within that scope and explain rejected or inapplicable findings.
The standalone sample findings are synthetic; they do not establish that an
implementation or defect exists.

An imported input binding supplies the selected node's full output through
`review-input.output`; it does not automatically select its findings artifact.

<review-report>
{{review-input.output}}
</review-report>

<coding-standards>
{{standards-input.output}}
</coding-standards>

Return applied fixes, rejected findings, validation commands and results, and
remaining risks. If you create or modify files during this invocation, end with
an exact `## Generated Files` section containing one Markdown link per line to
a concrete path relative to the project or workspace root, and no prose.
Report deleted files separately.

## implement.summary

Open the implementation report at `{{implement.execute.output_path}}` and
summarize the outcome without editing project files. Use its captured result-tree
links when file evidence is needed; do not rely on an old workspace path.

Include confirmed fixes, rejected or inapplicable findings, validation evidence,
and follow-up work. Do not present an unverified claim as a completed fix.
