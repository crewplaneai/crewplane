---
schema_version: "__SCHEMA_VERSION__"
name: Usage capture
nodes:
  - id: review.context
    mode: parallel
    providers: [claude, codex, gemini]
    findings: true
  - id: review.iterate
    mode: sequential
    needs: [review.context]
    depth: 2
    audit_rounds: 2
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
      - provider: gemini
        role: reviewer
  - id: review.summary
    mode: sequential
    needs: [review.iterate]
    providers: [claude]
---
## review.context
Return findings about this test input.
## review.iterate
Review {{review.context.findings}}.
## review.summary
Summarize {{review.iterate.output}}.
