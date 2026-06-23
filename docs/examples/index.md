# Example Templates

`orchestrator init` copies packaged workflow templates into
`.orchestrator/workflows/`.

Use the generated paths when running examples locally:

```bash
orchestrator run --tasks .orchestrator/workflows/code-review-example.task.md
orchestrator run --tasks .orchestrator/workflows/example-templates/feature-implement-example.task.md
```

The source-backed public examples are the packaged templates under
`src/orchestrator_cli/example_templates/`.

## Default Example

- [code-review-example.task.md](../../src/orchestrator_cli/example_templates/code-review-example.task.md)

## Workflow Library

- [feature-implement-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/feature-implement-example.task.md)
- [test-generation-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/test-generation-example.task.md)
- [refactoring-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/refactoring-example.task.md)
- [design-review-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/design-review-example.task.md)
- [multi-executor-review-chain-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/multi-executor-review-chain-example.task.md)

## Composition

- [review-findings-producer-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/composition/review-findings-producer-example.task.md)
- [review-fix-consumer-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/composition/review-fix-consumer-example.task.md)
- [review-fix-composed-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/composition/review-fix-composed-example.task.md)

See [composition examples](composition.md).

## Experimental Workspace

- [workspace-alternatives-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/worktree/workspace-alternatives-example.task.md)
- [workspace-inherited-worktree-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/worktree/workspace-inherited-worktree-example.task.md)

See [Experimental workspace examples](workspace.md).

## Sample Inputs

- [coding-standards.md](../../src/orchestrator_cli/example_templates/example-templates/sample-inputs/coding-standards.md)
- [feature-brief.md](../../src/orchestrator_cli/example_templates/example-templates/sample-inputs/feature-brief.md)
- [review-findings.md](../../src/orchestrator_cli/example_templates/example-templates/sample-inputs/review-findings.md)

Copy sample inputs into your own project or update generated workflow paths to
point at your real project files.
