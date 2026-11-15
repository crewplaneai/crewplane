---
schema_version: "__SCHEMA_VERSION__"
name: Project Memory Example
description: Review a feature brief with frozen accepted lessons and explicitly opt out the final handoff.
memory: true
nodes:
  - id: brief
    mode: input
    source: "{{file:.crewplane/workflows/example-templates/sample-inputs/feature-brief.md}}"
  - id: assess
    mode: sequential
    needs: [brief]
    depth: 1
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
  - id: handoff
    mode: parallel
    needs: [assess]
    memory: false
    providers: [codex]
---

<!-- Before validation: run crewplane memory init, configure codex and claude,
     and set settings.memory.extractor to a configured agent (or learn: false).
     Replace the sample brief with the current task. -->

## assess

Assess this feature brief against current project requirements. Treat recalled
lessons as historical evidence and recheck their conditions. Do not change files.

{{brief.output}}

<!-- crewplane:executor -->
Return a concise design assessment: scope, relevant current files, major risks,
validation needed, and any historical advice that no longer applies. Distinguish
observed facts from claims; do not report checks that were not run.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Check the assessment against the brief and current requirements. Identify stale
assumptions, unsupported verification claims, and missing acceptance criteria.
<!-- /crewplane:reviewer -->

## handoff

Open the complete assessment at `{{assess.output_path}}`. Return a short handoff
with the next action and unresolved concerns. Do not change project files.
This node opts out of direct recall and learning evidence; it can still see
historical information repeated in the assessment, so opt-out is not filtering.
