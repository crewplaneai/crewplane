# Composition Examples

Composition is useful after you have run a basic workflow and written one normal
workflow of your own. Use these examples when the same workflow pieces should be
shared across multiple root workflows.

These templates teach Markdown imports, alias-namespaced node IDs, parameters,
and input binding. They are not mock-safe with the default generated config:
the templates name real-provider agents such as `claude`, `codex`, and `gemini`.
To keep using mock output, adapt those provider names first. To run real CLIs,
enable the matching agents and switch the invoker to `cli`.

```text
producer module
      |
      v
consumer module
      |
      v
composed workflow
```

The example is split into three generated files:

- [review-findings-producer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-findings-producer-example.task.md)
- [review-fix-consumer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-consumer-example.task.md)
- [review-fix-composed-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-composed-example.task.md)

After `crewplane init`, adapt the provider names or enable the real providers,
then run the composed workflow:

```bash
crewplane run --tasks .crewplane/workflows/example-templates/composition/review-fix-composed-example.task.md
```

Adapt the examples by changing:

- imported `path` values to point at your reusable workflow modules
- `as` aliases to control namespaced node IDs
- `with` parameters for project-specific instructions
- `inputs` bindings to connect imported file-backed input nodes to local input
  nodes

Composition happens before runtime validation. The runtime sees the composed DAG,
not separate imported modules.
