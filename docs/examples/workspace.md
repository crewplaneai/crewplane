# Experimental Workspace Examples

Workspace examples show Experimental Git-backed source-tree isolation.

Packaged templates:

- [workspace-alternatives-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/worktree/workspace-alternatives-example.task.md)
- [workspace-inherited-worktree-example.task.md](../../src/orchestrator_cli/example_templates/example-templates/worktree/workspace-inherited-worktree-example.task.md)

After `orchestrator init`, run one explicitly:

```bash
orchestrator run --tasks .orchestrator/workflows/example-templates/worktree/workspace-alternatives-example.task.md
orchestrator run --tasks .orchestrator/workflows/example-templates/worktree/workspace-inherited-worktree-example.task.md
```

Before real execution, enable Experimental workspace support in
`.orchestrator/config.yml` and set an absolute
`settings.workspace.cache_root`.

The templates demonstrate:

- separate logical `worktree` checkouts
- `snapshot` checkouts
- `worktree: none` project-root opt-out
- implicit single-worktree selection
- optional branch export with `create_branch: true`

Experimental workspace isolation is not sandboxing. Provider CLIs still run
with their own configured permissions.
