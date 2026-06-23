# Workflow Composition

Markdown workflows can import other Markdown workflows. Composition happens
before runtime validation and execution.

## Import Syntax

```yaml
imports:
  - path: ./review-findings-producer-example.task.md
    as: quality.review
    with:
      focus: "security and correctness"
    inputs:
      standards: local.standards
```

Fields:

- `path`: Markdown workflow file to import.
- `as`: namespace alias for imported node IDs.
- `with`: string parameters for `{{param:key}}` substitution.
- `inputs`: bind declared child workflow inputs to local node IDs.

Imports are Markdown-only and must stay within the project root.

## Namespacing

Imported node IDs are prefixed with the import alias. If an imported workflow has
node `findings`, importing it as `quality.review` produces
`quality.review.findings`.

Dependencies and node artifact references are rewritten to the composed node IDs.

## Parameters

`{{param:key}}` exists only during composition. Bound parameters are substituted
before runtime validation. Unbound parameters are rewritten to `{{var:key}}`, so
the final runtime contract contains only supported runtime template forms.

## Inputs

A workflow can declare named inputs:

```yaml
inputs:
  standards: standards.file
nodes:
  - id: standards.file
    mode: input
    source: "{{file:docs/standards.md}}"
```

An importing workflow can bind that input to one of its own nodes. Bound imported
input nodes are pruned from the composed workflow.

## Examples

Packaged composition templates:

- [review-findings-producer-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/composition/review-findings-producer-example.task.md)
- [review-fix-consumer-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/composition/review-fix-consumer-example.task.md)
- [review-fix-composed-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/composition/review-fix-composed-example.task.md)
