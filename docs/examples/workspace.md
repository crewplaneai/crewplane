# Workspace Examples

These examples run provider work in managed Git worktrees and snapshots. Start
with the mock quickstart, then use these examples when you want to keep provider
edits out of your main working tree.

![Workspace isolation boundary diagram showing project root, workspace cache, provider process, `.crewplane/` artifacts, optional branch export, and a clear not-a-sandbox boundary.](../images/workspace/workspace-isolation-boundary.png)

Workspace isolation separates source-tree edits. Provider CLIs still run with
their own filesystem, network, and approval permissions.

## Prepare The Request And Repository

Both examples read `docs/crewplane-change-request.md` through an input node. Put
the request in that project file and commit it before running. Managed
workspaces start from recorded Git files, so an uncommitted request is not a
substitute.

1. Run `crewplane init` to create missing example files. Existing files are
   preserved; compare older copies with the linked templates below after an
   update.
2. If `docs/crewplane-change-request.md` does not exist, copy the generated
   sample brief there:

   ```bash
   mkdir -p docs
   cp .crewplane/workflows/example-templates/sample-inputs/feature-brief.md docs/crewplane-change-request.md
   ```

   If the destination already exists, review or edit it instead of overwriting
   it. The sample requests a small `normalize_spacing(text)` demonstration under
   `examples/text-cleanup/`. Replace it with your own request for real work.
3. In `.crewplane/config.yml`, enable `settings.workspace.enabled: true` and the
   `codex` and `claude` agent profiles used by these examples. Keep the invoker
   set to `mock` for a provider-free trial. To make real code changes, prepare
   those CLIs and switch to `cli` as described in
   [Provider setup](../getting-started/provider-setup.md).
4. Review and commit the request and any intended tracked config or workflow
   changes using your normal project process. Confirm that the working tree is
   clean before running with the default `clean_start: strict` setting.

Use an ordinary Git repository supported by the
[workspace support matrix](../guides/workspace-isolation.md#support-matrix).
The default cache location works without another setting; an optional
`settings.workspace.cache_root` must be an absolute path outside the project.

A mock run checks the workflow and writes sample reports without implementing
the request. Use real providers to evaluate the resulting code and tests.

## Compare Separate Implementations

[workspace-alternatives-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-alternatives-example.task.md)
gives the same request to a conservative implementation and an experimental
implementation. They use separate logical worktrees and can run independently.

```bash
crewplane validate .crewplane/workflows/example-templates/worktree/workspace-alternatives-example.task.md
crewplane run --tasks .crewplane/workflows/example-templates/worktree/workspace-alternatives-example.task.md
```

The comparison node reads both saved implementation reports from a disposable
snapshot. That snapshot does not contain either implementation's edits. Its
recommendation compares the reported changes, tests, and tradeoffs; it is not a
merged implementation or a direct test of both candidate code trees. The
conservative worktree also demonstrates optional local branch export.

## Continue One Implementation Through Testing

[workspace-inherited-worktree-example.task.md](../../src/crewplane/example_templates/example-templates/worktree/workspace-inherited-worktree-example.task.md)
declares one worktree. The implementation and test/fix nodes inherit it, so the
second node sees the first node's code changes. It also reads the first node's
saved report to identify the intended changes and validation work.

```bash
crewplane validate .crewplane/workflows/example-templates/worktree/workspace-inherited-worktree-example.task.md
crewplane run --tasks .crewplane/workflows/example-templates/worktree/workspace-inherited-worktree-example.task.md
```

The final `worktree: none` node runs in the project root and writes a handoff
from the saved reports. It does not receive the implementation's source edits
in the project root. The example exports the verified result as a local branch;
it does not switch branches, merge, or push.

## Inspect The Results

Read the result files and run summary first. For implementation evidence,
inspect the node's workspace records and Git bundles under
`.crewplane/execution-stages/<run-key>/`. Generated-file lists name only files
changed in that provider invocation; handoff reports summarize the whole result
separately.

See [Workspace isolation](../guides/workspace-isolation.md) for source history,
branch export, setup commands, and cleanup. Use
[workspace cleanup](../guides/cleanup.md) when you no longer need the managed
cache directories; canonical run records are retained separately.
