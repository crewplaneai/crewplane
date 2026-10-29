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
Use `needs` when the child should wait for specific nodes before its providers
start, without requiring a child input declaration.

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

Multiple public inputs may name the same fallback node. The fallback stays in
the composed workflow under the import namespace, such as `fix.standards-input`,
only if none of those names is bound. Binding any of them replaces the fallback
and redirects all references to it. If more than one is bound, they must resolve
to the same source node, including through nested imports. Conflicting sources
fail during composition regardless of YAML order.

## Examples

### Order an unchanged child workflow

Suppose you have three workflows in the same directory as the caller: a planning
workflow that produces `plan.final`, an implementation workflow that accepts
`implementation_plan` and produces `implement.handoff`, and a standalone
documentation workflow that starts at `review-fix-loop`. For these
illustrative filenames and node IDs, the caller can order them like this:

```yaml
imports:
  - path: write-implementation-plan.task.md
    as: plan

  - path: plan-implementation.task.md
    as: implement
    inputs:
      implementation_plan: plan.plan.final

  - path: review-fix-iteration.task.md
    as: review
    needs:
      - plan.plan.final
      - implement.implement.handoff
```

Composition gives `review.review-fix-loop` both prerequisites. If the child has
multiple independent provider branches, every entry branch waits. The
documentation workflow stays runnable on its own and needs no input declaration
for this ordering.

This only establishes order. It does not put the plan or implementation handoff
into the documentation review prompt. Supply any required review material
explicitly through prompts or declared inputs. File inputs retain their existing
resolution timing, and normal workspace lineage restrictions still apply. See
[Import prerequisites](../reference/workflow-syntax.md#import-prerequisites)
for the exact entry-node, namespace, validation, and workspace rules.

### Pass findings through input bindings

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

## Repeat a child for bounded data

Use `foreach` when the procedure is fixed and the number of items comes from a
literal collection or an upstream artifact. Declare ordinary inputs in the
child, bind one as `item_input`, and bind shared context with `inputs`. Static
imports inside that child continue to compose before execution.

A downstream node depends on the group ID and reads its ordered result index.
It can follow result descriptors to individual terminal outputs. A second
foreach group can consume that index as its collection. The parent remains the
run boundary, including when the child declares repetition metadata.

Start with the [folder and chunk examples](../examples/composition.md#iterate-over-folders).
The [syntax reference](../reference/workflow-syntax.md#foreach-nodes) defines the
limits and binding rules; the [artifact reference](../reference/artifacts.md#foreach-expansions)
explains checkpoint recovery and force reruns.

## Next

Optionally continue to [Workspace Isolation](workspace-isolation.md)
when workflows need isolated source-tree edits.

Otherwise, skip to
[Mock Validation](mock-validation.md).

Or return to the [Guides](../index.md#guided-tutorial-track).
