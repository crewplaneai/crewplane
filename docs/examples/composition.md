# Composition Examples

Composition examples show how to reuse workflow modules with imports, aliases,
parameters, and bound inputs.

Packaged templates:

- [review-findings-producer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-findings-producer-example.task.md)
- [review-fix-consumer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-consumer-example.task.md)
- [review-fix-composed-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-composed-example.task.md)

After `crewplane init`, run the composed example with:

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
