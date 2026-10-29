---
name: Bounded chunks
nodes:
  - id: request
    mode: input
    source: "{{file:request.txt}}"
  - id: planner
    mode: parallel
    needs: [request]
    providers: [alpha]
    findings: true
  - id: chunks
    mode: foreach
    needs: [planner]
    workflow: child.md
    item_input: item
    inputs: {request: request}
    with: {style: concise}
    items: "{{planner.findings}}"
    max_items: 4
  - id: summarize
    mode: parallel
    needs: [chunks]
    providers: [alpha]
---
## planner
Plan chunks for {{request.output}}. Return a JSON array in findings.
## chunks
<!-- The child is compiled before the planner runs. -->
## summarize
Summarize the result index {{chunks.output}}.
