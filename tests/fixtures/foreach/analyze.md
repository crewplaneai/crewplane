---
name: Analyze chunk
inputs: {item: item, request: shared}
nodes:
  - {id: item, mode: input, source: "{{file:absent-item.json}}"}
  - {id: shared, mode: input, source: "{{file:absent-request.txt}}"}
  - id: analyze
    mode: parallel
    needs: [item, shared]
    providers: [alpha]
    findings: true
---
## analyze
Use a {{param:style}} style for {{item.output}} with request {{shared.output}}.
