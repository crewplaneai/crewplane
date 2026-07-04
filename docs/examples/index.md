# Examples

After `crewplane init`, you have one mock-safe workflow at
`.crewplane/workflows/single-agent-review.task.md` and additional examples under
`.crewplane/workflows/example-templates/`.

Start with the mock-safe workflow. For the other examples, either change their
provider names to match configured mock-safe agents or enable the real provider
agents in `.crewplane/config.yml` and switch the invoker to `cli`.

## Suggested Path

1. Run `single-agent-review.task.md` first. It uses the generated `mock` agent
   and leaves result and findings files you can inspect immediately.
2. Move to `code-review-example.task.md` after you have provider names that
   match your config.
3. Use the implementation, refactoring, design-review, and test-generation
   examples when you are ready to run real provider CLIs.
4. Use composition and workspace examples only after the basic workflow shape is
   familiar.

Once `crewplane init` has generated the workflow copies, these commands run the
most common examples:

```bash
crewplane run --tasks .crewplane/workflows/single-agent-review.task.md
crewplane run --tasks .crewplane/workflows/example-templates/code-review-example.task.md
crewplane run --tasks .crewplane/workflows/example-templates/feature-implement-example.task.md
```

Only `single-agent-review.task.md` is generated at the top level. The other
examples live under `example-templates/`, so they need an explicit `--tasks`
path. Add `--no-live` only when you want a plain terminal run without the live
dashboard.

The source-backed public examples are the packaged templates under
`src/crewplane/example_templates/`.

## Default Example

- [single-agent-review.task.md](../../src/crewplane/example_templates/single-agent-review.task.md)

```bash
crewplane run --tasks .crewplane/workflows/single-agent-review.task.md
```

## Workflow Library

- [code-review-example.task.md](../../src/crewplane/example_templates/example-templates/code-review-example.task.md)
- [feature-implement-example.task.md](../../src/crewplane/example_templates/example-templates/feature-implement-example.task.md)
- [test-generation-example.task.md](../../src/crewplane/example_templates/example-templates/test-generation-example.task.md)
- [refactoring-example.task.md](../../src/crewplane/example_templates/example-templates/refactoring-example.task.md)
- [design-review-example.task.md](../../src/crewplane/example_templates/example-templates/design-review-example.task.md)
- [multi-executor-review-chain-example.task.md](../../src/crewplane/example_templates/example-templates/multi-executor-review-chain-example.task.md)

```bash
crewplane run --tasks .crewplane/workflows/example-templates/code-review-example.task.md
```

## Composition

- [review-findings-producer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-findings-producer-example.task.md)
- [review-fix-consumer-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-consumer-example.task.md)
- [review-fix-composed-example.task.md](../../src/crewplane/example_templates/example-templates/composition/review-fix-composed-example.task.md)

See [composition examples](composition.md).

```bash
crewplane run --tasks .crewplane/workflows/example-templates/composition/review-fix-composed-example.task.md
```

## Experimental Workspace

- [workspace-alternatives-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-alternatives-example.task.md)
- [workspace-inherited-worktree-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-inherited-worktree-example.task.md)

See [Experimental workspace examples](workspace.md).

```bash
crewplane run --tasks .crewplane/workflows/example-templates/worktree/workspace-alternatives-example.task.md
```

## Sample Inputs

- [coding-standards.md](../../src/crewplane/example_templates/example-templates/sample-inputs/coding-standards.md)
- [feature-brief.md](../../src/crewplane/example_templates/example-templates/sample-inputs/feature-brief.md)
- [review-findings.md](../../src/crewplane/example_templates/example-templates/sample-inputs/review-findings.md)

Copy sample inputs into your own project or update generated workflow paths to
point at your real project files.
