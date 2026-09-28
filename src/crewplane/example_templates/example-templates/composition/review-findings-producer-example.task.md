---
schema_version: "__SCHEMA_VERSION__"
name: Review Findings Producer Example
description: Produce a concise findings artifact that other workflows can import and consume.
nodes:
  - id: review.findings
    mode: parallel
    findings: true
    providers: [gemini]
---

## review.findings

Review `{{param:project_name}}` without editing project files. Inspect the
project's instructions and relevant implementation and tests, then report
concrete correctness or regression risks with file references. Distinguish
verified defects from unverified concerns.

Keep the complete report concise because an imported consumer receives this
node's full output. End with exactly one non-empty findings block:
<!-- findings -->
- concrete finding with file reference, risk, and recommended fix
<!-- /findings -->

If no actionable issues are found, put `No actionable findings.` inside that
block rather than inventing a defect.
