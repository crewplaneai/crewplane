# Examples

After `crewplane init`, the starter workflow is at
`.crewplane/workflows/single-agent-review.task.md` and the library is under
`.crewplane/workflows/example-templates/`.

Start with the [quickstart](../getting-started/quickstart.md), then choose an
example below. The generated
[example README](../../src/crewplane/example_templates/example-templates/README.md)
lists required agent profiles, inputs, and expected outcomes.

## Default Example

[Single-agent review](../../src/crewplane/example_templates/single-agent-review.task.md)
uses the generated mock config and produces a report and findings.

## Workflow Library

- [Code review](../../src/crewplane/example_templates/example-templates/code-review-example.task.md)
- [Feature implementation](../../src/crewplane/example_templates/example-templates/feature-implement-example.task.md)
- [Fix a reported bug](bug-fix.md)
- [Review an existing change](review-existing-change.md)
- [Test generation](../../src/crewplane/example_templates/example-templates/test-generation-example.task.md)
- [Refactoring](../../src/crewplane/example_templates/example-templates/refactoring-example.task.md)
- [Design review](../../src/crewplane/example_templates/example-templates/design-review-example.task.md)

Enable the profiles named by your chosen workflow using
[Provider setup](../getting-started/provider-setup.md#turn-mock-mode-onoff).
Keep the invoker set to `mock` for a trial with sample responses. Library examples
need an explicit `--tasks` path; run these commands from your project directory:

```bash
crewplane validate .crewplane/workflows/example-templates/code-review-example.task.md
crewplane run --no-live --tasks .crewplane/workflows/example-templates/code-review-example.task.md
```

Before relying on reviewer approval, choose a
[review exhaustion policy](../guides/review-loops.md#continue-or-fail-on-exhaustion).
For reports and captured files, see
[Inspecting run records](../guides/inspecting-artifacts.md).

## Composition

[Composed review and fix](composition.md) connects a reusable findings producer
to a fix consumer. The walkthrough covers the recipe, standalone modules, and
input bindings.

## Workspace

Follow [Workspace examples](workspace.md) to prepare the repository and committed
change request before running either template:

- [Workspace alternatives](../../src/crewplane/example_templates/example-templates/worktree/workspace-alternatives-example.task.md): implement the request separately and compare the reports.
- [Inherited worktree](../../src/crewplane/example_templates/example-templates/worktree/workspace-inherited-worktree-example.task.md): implement, test, and fix on one source line.

## Advanced Patterns

[Multiple executors](../../src/crewplane/example_templates/example-templates/multi-executor-review-chain-example.task.md)
produces two design proposals for review together. See
[provider order](../guides/review-loops.md#provider-order) for how
executor outputs reach reviewers.

## Sample Inputs

- [Coding standards](../../src/crewplane/example_templates/example-templates/sample-inputs/coding-standards.md)
- [Feature brief](../../src/crewplane/example_templates/example-templates/sample-inputs/feature-brief.md)
- [Bug report](../../src/crewplane/example_templates/example-templates/sample-inputs/bug-report.md)
- [Change review brief](../../src/crewplane/example_templates/example-templates/sample-inputs/change-review-brief.md)
- [Review findings](../../src/crewplane/example_templates/example-templates/sample-inputs/review-findings.md)

Replace sample inputs with your project's scope and acceptance criteria.
The feature brief defines a `normalize_spacing(text)` exercise; test generation
requires an existing implementation. The sample bug report and review findings
are hypothetical, and existing-change review requires an actual patch. Each
recipe describes its prerequisites.
