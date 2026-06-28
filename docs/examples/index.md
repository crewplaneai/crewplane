# Examples

`crewplane init` copies the default workflow to `.crewplane/workflows/` and
additional examples to `.crewplane/workflows/example-templates/`.

Start with the default workflow. Use the others after provider setup or after
changing their provider names to match your config.

| Example | Use when | Requires |
| --- | --- | --- |
| `single-agent-review.task.md` | First mock or first real review. | mock or one provider. |
| `code-review-example.task.md` | Multi-provider code review. | provider setup. |
| `feature-implement-example.task.md` | Implementation workflow. | provider setup. |
| `test-generation-example.task.md` | Generate tests. | provider setup. |
| `multi-executor-review-chain-example.task.md` | Compare executors then review. | multiple providers. |
| Composition examples | Reuse workflow modules. | imports. |
| Workspace examples | Isolate source-tree edits. | Experimental workspace support. |

Copy-paste run commands use generated paths:

```bash
crewplane run --tasks .crewplane/workflows/single-agent-review.task.md
crewplane run --tasks .crewplane/workflows/example-templates/code-review-example.task.md
crewplane run --tasks .crewplane/workflows/example-templates/feature-implement-example.task.md
```

Only `single-agent-review.task.md` is top-level after `crewplane init`; nested
examples require explicit `--tasks` and may require provider setup.

The source-backed public examples are the packaged templates under
`src/crewplane/example_templates/`.

## Default Example

- [single-agent-review.task.md](../../src/crewplane/example_templates/single-agent-review.task.md)

```bash
crewplane run --tasks .crewplane/workflows/single-agent-review.task.md --no-live
```

## Workflow Library

- [code-review-example.task.md](../../src/crewplane/example_templates/example-templates/code-review-example.task.md)
- [feature-implement-example.task.md](../../src/crewplane/example_templates/example-templates/feature-implement-example.task.md)
- [test-generation-example.task.md](../../src/crewplane/example_templates/example-templates/test-generation-example.task.md)
- [refactoring-example.task.md](../../src/crewplane/example_templates/example-templates/refactoring-example.task.md)
- [design-review-example.task.md](../../src/crewplane/example_templates/example-templates/design-review-example.task.md)
- [multi-executor-review-chain-example.task.md](../../src/crewplane/example_templates/example-templates/multi-executor-review-chain-example.task.md)

```bash
crewplane run --tasks .crewplane/workflows/example-templates/code-review-example.task.md --no-live
```

## Composition

- [review-findings-producer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-findings-producer-example.task.md)
- [review-fix-consumer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-consumer-example.task.md)
- [review-fix-composed-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-composed-example.task.md)

See [composition examples](composition.md).

```bash
crewplane run --tasks .crewplane/workflows/example-templates/composition/review-fix-composed-example.task.md --no-live
```

## Experimental Workspace

- [workspace-alternatives-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-alternatives-example.task.md)
- [workspace-inherited-worktree-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-inherited-worktree-example.task.md)

See [Experimental workspace examples](workspace.md).

```bash
crewplane run --tasks .crewplane/workflows/example-templates/worktree/workspace-alternatives-example.task.md --no-live
```

## Sample Inputs

- [coding-standards.md](../../src/crewplane/example_templates/example-templates/sample-inputs/coding-standards.md)
- [feature-brief.md](../../src/crewplane/example_templates/example-templates/sample-inputs/feature-brief.md)
- [review-findings.md](../../src/crewplane/example_templates/example-templates/sample-inputs/review-findings.md)

Copy sample inputs into your own project or update generated workflow paths to
point at your real project files.
