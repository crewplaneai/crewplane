---
schema_version: "__SCHEMA_VERSION__"
name: Design Review Example
description: Compare design options, iterate with reviewer feedback, and record a decision.
nodes:
  - id: design.discovery
    mode: parallel
    providers: [claude]
  - id: design.iteration
    mode: sequential
    needs: [design.discovery]
    depth: 2
    providers:
      - provider: codex
        role: executor
      - provider: gemini
        role: reviewer
  - id: design.decision
    mode: parallel
    needs: [design.iteration]
    providers: [claude]
---

## design.discovery

Draft a design options brief for `{{var:project_name}}`.

Use this feature brief as the concrete target:
{{file:.crewplane/workflows/example-templates/sample-inputs/feature-brief.md}}

Return 2-3 design options with tradeoffs and a recommended starting point.
Include the feature brief's acceptance criteria and relevant project constraints
so later reviews can check them. Inspect relevant project files, but do not
change them.

## design.iteration

Open the complete discovery brief at `{{design.discovery.output_path}}`.
Preserve its acceptance criteria and relevant constraints when assessing the
current design.

<!-- crewplane:executor -->
Revise the design for correctness, risk reduction, and maintainability.
Return the full updated design in each response. Do not change project files.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Review the current design for correctness, risks, maintainability, and test
strategy. Identify any acceptance criteria or concrete interface details that
were lost during revision.
<!-- /crewplane:reviewer -->

## design.decision

Open the discovery brief at `{{design.discovery.output_path}}` and the complete
reviewed design at `{{design.iteration.output_path}}`. Produce a final decision
record without changing project files.

Return the full decision record in this response; do not only summarize or point to
a file. Include selected option, rationale, and implementation milestones, and
preserve accepted concrete interface, data-shape, and test details from the
reviewed iteration output. State whether reviewers approved and list unresolved
objections; do not label an unapproved proposal as an accepted decision.
