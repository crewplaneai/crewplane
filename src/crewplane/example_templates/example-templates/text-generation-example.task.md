---
schema_version: "__SCHEMA_VERSION__"
name: Text Generation Example
description: Summarize supplied content with a local text model and refine the answer.
nodes:
  - id: upstream
    mode: sequential
    providers: [local]
  - id: refine
    mode: sequential
    needs: [upstream]
    providers: [local]
---

## upstream

Summarize the following feature brief in five concise bullet points. Use only the
supplied content; identify any missing information.

{{file:.crewplane/workflows/example-templates/sample-inputs/feature-brief.md}}

## refine

Turn this summary into a short plain-language explanation for a new teammate.
Preserve its stated limitations:

{{upstream.output}}
