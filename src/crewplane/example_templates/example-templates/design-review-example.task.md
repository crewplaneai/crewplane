---
schema_version: "__SCHEMA_VERSION__"
name: Design Review Example
description: Compare design options, iterate with reviewer feedback, and record a decision.
nodes:
  - id: design.discovery
    mode: sequential
    providers: [claude]
  - id: design.iteration
    mode: sequential
    needs: [design.discovery]
    depth: 2
    providers:
      - provider: codex
        model: gpt-5.4
        role: executor
      - provider: gemini
        role: reviewer
  - id: design.decision
    mode: sequential
    needs: [design.iteration]
    token_budget:
      warn_threshold_chars: 25000
      fail_threshold_chars: 70000
    providers: [claude]
---

## design.discovery

Draft a design options brief for `{{var:project_name}}`.

Use this feature brief as the concrete target:
{{file:.crewplane/workflows/example-templates/sample-inputs/feature-brief.md}}

Return 2-3 design options with tradeoffs.

## design.iteration

Iterate on the preferred design using this discovery output:
{{design.discovery.output}}

<!-- crewplane:executor -->
Revise the design for correctness, risk reduction, and maintainability.
Return the full updated design in each response.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Review the current design for correctness, risks, maintainability, and test
strategy. Do not edit files.

End with the structured review sections and a final verdict token.
<!-- /crewplane:reviewer -->

## design.decision

Produce a final design decision record from:
{{design.discovery.output}}

Reviewed design artifact:
- Path: {{design.iteration.output_path}}
- Size: {{design.iteration.output_size}}
- SHA-256: {{design.iteration.output_sha256}}

Return the full decision record in this response; do not only summarize or point to
a file. Include selected option, rationale, and implementation milestones, and
preserve accepted concrete interface, data-shape, and test details from the
reviewed iteration output.

If you create or update a repository file for the decision record, include a
`Generated Files` section that lists the workspace-relative path.
