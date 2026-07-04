# Experimental Workspace Examples

Workspace examples show how Crewplane can run provider work against managed Git
worktrees and snapshots. Treat them as advanced examples: run the mock
quickstart and one normal provider workflow before using workspace isolation.

![Workspace isolation boundary diagram showing project root, workspace cache, provider process, `.crewplane/` artifacts, optional branch export, and a clear not-a-sandbox boundary.](../images/workspace-isolation-boundary.png)

The examples demonstrate source-tree isolation with managed worktrees and
snapshots. They do not sandbox provider CLIs.

Before running these examples, make sure you have:

- an ordinary Git repository compatible with `blob_exact`
- workspace support enabled in `.crewplane/config.yml`
- real provider agents enabled with an invoker that honors runtime working
  directories

The templates demonstrate logical `worktree` and `snapshot` source lines,
`worktree: none`, inherited worktree selection, and optional branch export. They
are for real provider-backed workspace behavior, not the default mock-only first
run.

Packaged templates:

- [workspace-alternatives-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-alternatives-example.task.md)
- [workspace-inherited-worktree-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-inherited-worktree-example.task.md)

Before running them, enable Experimental workspace support in
`.crewplane/config.yml`. You may set an absolute `settings.workspace.cache_root`;
when it is omitted, Crewplane uses the platform cache directory.

Then run one generated template explicitly:

```bash
crewplane run --tasks .crewplane/workflows/example-templates/worktree/workspace-alternatives-example.task.md
crewplane run --tasks .crewplane/workflows/example-templates/worktree/workspace-inherited-worktree-example.task.md
```

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
