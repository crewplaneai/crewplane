---
schema_version: "__WORKFLOW_SCHEMA_VERSION__"
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
    source: "{{file:.orchestrator/inputs/coding-standards.md}}"
  - id: handoff.final
    mode: sequential
    needs: [fix.implement.summary]
    providers: [claude]
---

## handoff.final

Create a final handoff from:

Imported findings:
- Path: {{quality.review.findings.findings_path}}
- Size: {{quality.review.findings.findings_size}}
- SHA-256: {{quality.review.findings.findings_sha256}}

Implementation summary:
{{fix.implement.summary.output}}

Call out which inputs were bound through `imports[].inputs`, how
`imports[].with` scoped the producer, and what follow-up work remains.
