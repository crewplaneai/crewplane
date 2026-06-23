---
schema_version: "__SCHEMA_VERSION__"
name: Review Findings Producer Example
description: Produce a concise findings artifact that other workflows can import and consume.
nodes:
  - id: review.findings
    mode: sequential
    findings: true
    providers: [gemini]
---

## review.findings

Review `{{param:project_name}}` and produce concise implementation findings.

Return a short report, then end with exactly one findings block:
<!-- findings -->
- concrete finding with file reference, risk, and recommended fix
<!-- /findings -->
