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
