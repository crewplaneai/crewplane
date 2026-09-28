---
schema_version: "__SCHEMA_VERSION__"
name: Review Fix Composed Example
description: Compose reusable workflows, bind imported inputs, and summarize the handoff.
imports:
  - path: review-findings-producer-example.task.md
    as: quality
    with:
      project_name: composed-review-fix-example
  - path: review-fix-consumer-example.task.md
    as: fix
    inputs:
      review_input: quality.review.findings
      standards_input: handoff.standards
nodes:
  - id: handoff.standards
    mode: input
    source: "{{file:.crewplane/workflows/example-templates/sample-inputs/coding-standards.md}}"
  - id: handoff.final
    mode: parallel
    needs: [fix.implement.summary]
    providers: [claude]
---

## handoff.final

Create a final handoff without editing project files. Open these artifacts:

- Concise review findings: `{{quality.review.findings.findings_path}}`
- Implementation summary: `{{fix.implement.summary.output_path}}`

Report confirmed fixes, rejected findings, validation evidence, and follow-up
work. Use captured result-tree links when citing generated files.

Explain that `imports[].inputs` bound the producer node and the standards input
to the consumer. The consumer received the producer's full output, not an
automatic selection of its findings artifact; this handoff opens the separate
findings artifact explicitly. Note the project label supplied by
`imports[].with`.
