---
name: Chunk procedure
inputs: {item: item, request: shared}
imports:
  - path: analyze.md
    as: part
    inputs: {item: item, request: shared}
nodes:
  - {id: item, mode: input, source: "{{file:absent-item.json}}"}
  - {id: shared, mode: input, source: "{{file:absent-request.txt}}"}
---
