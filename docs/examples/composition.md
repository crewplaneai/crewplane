# Composed Review and Fix

Use this recipe after running a basic workflow, when you want to reuse review
and fix steps in other workflows.

This recipe reviews a project, passes the report to a fix workflow, and writes
a final handoff. It demonstrates Markdown imports, named parameters, and input
binding. It names the `claude`, `codex`, and `gemini` agent profiles, which are
commented out in the generated config. Uncomment those profiles and
keep the invoker set to `mock` to try the composition without starting provider
CLIs. Switch the invoker to `cli` only when you want real provider work. See
[Provider setup](../getting-started/provider-setup.md#turn-mock-mode-onoff).

Run [review-fix-composed-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-composed-example.task.md)
as the recipe. It uses two supporting modules:

- [review-findings-producer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-findings-producer-example.task.md)
  inspects the project and returns a report with findings.
- [review-fix-consumer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-consumer-example.task.md)
  checks the findings against the project and applies supported fixes.

The composed workflow connects them as review → verify findings and fix →
handoff. It does not add an executor/reviewer approval gate.

After `crewplane init`, configure the matching agent names, then validate and run
the composed workflow:

```bash
crewplane validate .crewplane/workflows/example-templates/composition/review-fix-composed-example.task.md
crewplane run --tasks .crewplane/workflows/example-templates/composition/review-fix-composed-example.task.md
```

When the consumer runs alone, its fallback input is the generated sample
findings file. Those findings are synthetic, so the executor must verify each
issue before changing files. In the composed workflow, the producer's full
report, including its findings block, replaces that fallback input. Input
binding passes the producer's full output; it does not select the separate
findings artifact automatically.

Adapt the examples by changing:

- imported `path` values to point at your reusable workflow modules
- `as` aliases to control namespaced node IDs
- `with` parameters for project-specific instructions
- `inputs` bindings to connect imported file-backed input nodes to local input
  nodes

Composition happens before runtime validation. The runtime sees the composed DAG,
not separate imported modules.

## Iterate over folders

Configure the `codex` agent profile and a finite concurrency cap, for example
`settings.max_concurrent_nodes: 4`. Save the files below in `.crewplane/workflows/`.
Folder names are data supplied to a fixed procedure. This literal collection needs
no `max_items`; its length supplies the bound.

`folder-audit.task.md`:

```yaml
---
name: Folder audits
nodes:
  - id: folders
    mode: foreach
    workflow: audit-folder.task.md
    item_input: item
    items:
      - {folder: src}
      - {folder: tests}
---
## folders
<!-- Audit each declared folder. -->
```

`audit-folder.task.md`:

```yaml
---
name: Audit one folder
inputs: {item: item}
nodes:
  - id: item
    mode: input
    source: "{{file:folder.json}}"
  - id: audit
    mode: parallel
    needs: [item]
    providers: [codex]
---
## audit
Audit the folder described by this JSON object and report supported findings:
{{item.output}}
```

The bound item slot replaces `folder.json`; that file is only required when
running the child alone. The two instances become `folders.i0.audit` and
`folders.i1.audit`. `folders-result.md` contains an ordered index of their output
artifacts. Validation and preview do not start workflow nodes or create run artifacts:

```bash
crewplane validate .crewplane/workflows/folder-audit.task.md
crewplane run --tasks .crewplane/workflows/folder-audit.task.md --dry-run
```

Then run the workflow:

```bash
crewplane run --tasks .crewplane/workflows/folder-audit.task.md
```

## Analyze dynamically planned chunks

Place `request.md` in the project root with the source material and analysis
requirements. Keep the finite concurrency setting from the folder example.
A single executor emits the collection inside its findings block. Its ordinary
Markdown output is not a collection source.
The artifact reference requires `max_items` because its collection is resolved at
runtime; a collection exceeding the cap fails before any child starts.

`chunk-analysis.task.md`:

```yaml
---
name: Chunk analysis
nodes:
  - id: request
    mode: input
    source: "{{file:request.md}}"
  - id: split
    mode: parallel
    needs: [request]
    providers: [codex]
    findings: true
  - id: chunks
    mode: foreach
    needs: [split]
    workflow: analyze-chunk.task.md
    item_input: item
    inputs: {request: request}
    with: {style: concise}
    items: "{{split.findings}}"
    max_items: 12
  - id: summarize
    mode: parallel
    needs: [chunks]
    providers: [codex]
---
## split
Divide the supplied material into at most twelve chunks:
{{request.output}}
Return an array of objects with a chunk label and its excerpt, as strict JSON
inside <!-- findings --> and <!-- /findings -->. Do not use a code fence.
## chunks
<!-- One fixed analysis procedure per chunk. -->
## summarize
Use this ordered index to read the terminal outputs and summarize the analysis.
Resolve each descriptor path relative to the directory containing the index at
{{chunks.output_path}}.
{{chunks.output}}
```

`analyze-chunk.task.md`:

```yaml
---
name: Analyze one chunk
inputs: {item: item, request: request}
nodes:
  - {id: item, mode: input, source: "{{file:chunk.json}}"}
  - {id: request, mode: input, source: "{{file:request.md}}"}
  - id: analyze
    mode: parallel
    needs: [item, request]
    providers: [codex]
---
## analyze
Use a {{param:style}} style. Analyze the supplied chunk:
{{item.output}}
Follow the shared requirements:
{{request.output}}
```

The caller supplies both inputs, so their child fallback files are not read.
Only item data varies at runtime; `style`, provider selection, prompts, and child
imports are compiled before `split` executes. For a mock run of the dynamic
example, use file-mode mock fixtures whose `split` findings contain a JSON array;
the default mock findings are not a JSON array. See [Mock Validation](../guides/mock-validation.md).

Run the workflow with:

```bash
crewplane run --tasks .crewplane/workflows/chunk-analysis.task.md
```

The `chunks` result contains the ordered index, and `summarize` produces the
final summary.

See [Foreach nodes](../reference/workflow-syntax.md#foreach-nodes) for limits and
[expansion artifacts](../reference/artifacts.md#foreach-expansions) for result
paths, interrupted-run recovery, skipping, and force behavior.
