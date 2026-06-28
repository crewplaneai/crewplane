# Experimental Workspace Examples

Workspace examples show Experimental Git-backed source-tree isolation.

These examples are advanced. Run the mock quickstart and a normal provider
workflow before using Experimental workspace isolation.

Prerequisites:

- a Git repository
- workspace support enabled in `.crewplane/config.yml`
- an ordinary Git repository compatible with `blob_exact`
- a provider CLI that works directly from your shell

Packaged templates:

- [workspace-alternatives-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-alternatives-example.task.md)
- [workspace-inherited-worktree-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-inherited-worktree-example.task.md)

After `crewplane init`, run one explicitly:

```bash
crewplane run --tasks .crewplane/workflows/example-templates/worktree/workspace-alternatives-example.task.md
crewplane run --tasks .crewplane/workflows/example-templates/worktree/workspace-inherited-worktree-example.task.md
```

Before real execution, enable Experimental workspace support in
`.crewplane/config.yml`. You may set an absolute `settings.workspace.cache_root`;
when it is omitted, Crewplane uses the platform cache directory.

The templates demonstrate:

- separate logical `worktree` checkouts
- `snapshot` checkouts
- `worktree: none` project-root opt-out
- implicit single-worktree selection
- optional branch export with `create_branch: true`

Experimental workspace isolation is not sandboxing. Provider CLIs still run
with their own configured permissions.

See [Experimental workspace isolation](../guides/workspace-isolation.md) for
the setup flow, support matrix, safety boundaries, and cleanup behavior.
