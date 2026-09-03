# Workflow Composition

Markdown workflows can import other Markdown workflows. Composition happens
before runtime validation and execution.

Only read this after you have written one normal workflow. Composition is for
reusable workflow modules: shared review patterns, common inputs, or workflows
that should be assembled from smaller pieces.

```text
root workflow
  imports review producer as quality
        |
        v
composed DAG
  quality.review.findings
```

## Import Syntax

```yaml
imports:
  - path: ./review-findings-producer-example.task.md
    as: quality
    with:
      project_name: "current project"
```

In this example, `path` points to the reusable workflow, `as` gives every
imported node a namespace, and `with` supplies the producer's
`{{param:project_name}}` value. The optional `inputs` field, covered below,
connects a child workflow input to a node in the parent workflow.

Import paths resolve relative to the workflow file that declares them. Imports
are Markdown-only and must stay within the project root. Duplicate aliases fail,
and unused `with` parameters fail so misspelled parameter names do not silently
disappear.

## Namespacing

Imported node IDs are prefixed with the import alias. If an imported workflow has
node `review.findings`, importing it as `quality` produces
`quality.review.findings`.

Dependencies and node artifact references are rewritten to the composed node IDs.
Imported worktree declarations and selectors are alias-qualified too.
`worktree: none` remains the project-root opt-out. Implicit single-worktree
inheritance is resolved during composition.

## Parameters

`{{param:key}}` exists only during composition. Bound parameters are substituted
before runtime validation. Unbound parameters are rewritten to `{{var:key}}`, so
the final runtime contract contains only supported runtime template forms.

## Inputs

A reusable workflow can declare named inputs. Think of each input as a
replaceable dependency:

```yaml
inputs:
  standards_input: standards-input
nodes:
  - id: standards-input
    mode: input
    source: "{{file:docs/standards.md}}"
```

This means:

- `standards_input` is the public input name.
- `standards-input` is the workflow's local fallback input node.
- If this workflow runs by itself, Crewplane uses `standards-input`.

When another workflow imports it, the importer can bind that input name to a node
that is visible in the importing workflow:

```yaml
imports:
  - path: ./review-fix-consumer-example.task.md
    as: fix
    inputs:
      standards_input: handoff.standards
```

During composition, import `inputs` act as overrides. The binding replaces the
fallback input node:

> ⚠️ **Note:** When an importing workflow binds an input, that upstream binding
> overrides the imported workflow's fallback input node.

- `standards-input` is removed from the imported workflow.
- Dependencies on `standards-input` are rewritten to `handoff.standards`.
- `{{standards-input.output}}` becomes `{{handoff.standards.output}}`.

If an input is not bound by the importer, the fallback input node stays in the
composed workflow under the import namespace, such as `fix.standards-input`.

## Examples

The packaged producer defines `review.findings` with `findings: true`. When you
import it as `quality`, the composed node becomes `quality.review.findings`.
Downstream prompts can use its findings with:

```markdown
Use the review findings:

{{quality.review.findings.findings}}
```

Packaged composition templates:

- [review-findings-producer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-findings-producer-example.task.md)
- [review-fix-consumer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-consumer-example.task.md)
- [review-fix-composed-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-composed-example.task.md)

## Next

Optionally continue to [Workspace Isolation](workspace-isolation.md)
when workflows need isolated source-tree edits.

Otherwise, skip to
[Mock Validation](mock-validation.md).

Or return to the [Guides](../index.md#guided-tutorial-track).
