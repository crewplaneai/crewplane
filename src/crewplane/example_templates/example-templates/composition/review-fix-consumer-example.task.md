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
    mode: sequential
    needs: [review-input, standards-input]
    token_budget:
      warn_threshold_chars: 20000
      fail_threshold_chars: 60000
    providers: [codex]
  - id: implement.summary
    mode: sequential
    needs: [implement.execute]
    providers: [claude]
---

## implement.execute

Use these review findings as raw input:
{{review-input.output}}

Use these coding standards as additional context:
{{standards-input.output}}

Apply the fixes you agree with and explain any findings you reject.

If files change, include a link-only `Generated Files` section.

## implement.summary

Summarize the implementation outcome from:

Implementation artifact:
- Path: {{implement.execute.output_path}}
- Size: {{implement.execute.output_size}}
- SHA-256: {{implement.execute.output_sha256}}

Include applied fixes, rejected findings, and any follow-up work.
