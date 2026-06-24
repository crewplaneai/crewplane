---
schema_version: "__SCHEMA_VERSION__"
name: Single Agent Review
description: One deterministic mock review node for the first Crewplane run.
nodes:
  - id: review.project
    mode: parallel
    findings: true
    providers: [mock]
---

## review.project

Review this project for first-run readiness.

Use the generated Crewplane configuration as context:
{{file:.crewplane/config.yml}}

Return:
1. the most important setup or documentation issue
2. one concrete follow-up check
3. a short note on where run artifacts should be inspected

End with exactly one concise findings block:
<!-- findings -->
- finding with affected file, behavior, and recommended next action
<!-- /findings -->
