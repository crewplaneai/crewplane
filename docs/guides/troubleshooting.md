# Troubleshooting

## Supported Platforms

Crewplane supports Linux, macOS, and WSL. Native Windows is not supported:
process liveness and lock recovery rely on POSIX process checks. On Windows,
run Crewplane inside WSL. See
[Installation](../getting-started/installation.md).

## Start By Symptom

| Symptom | Start here |
| --- | --- |
| Command not found | [Installation](../getting-started/installation.md). |
| No workflow found | [Default discovery](running-workflows.md#default-discovery). |
| Provider not found | [Provider setup](../getting-started/provider-setup.md). |
| Run skipped | [Duplicate skip](running-workflows.md#duplicate-skip). |
| Run resumed | [Resume](running-workflows.md#resume). |
| Run lock unavailable | [Run lock unavailable](#run-lock-unavailable). |
| Windows launcher or file error | [Native Windows launch and file errors](#native-windows-launch-and-file-errors). |
| No dashboard | [tmux missing](#tmux-missing) or [Watch Runs Live and Inspect Results](watch-runs-live-and-inspect-results.md). |
| Need help | [Reproducible support bundle](reproducible-support-bundle.md). |

## Inspect Run Artifacts

Use terminal output to identify the run key, then start with
`.crewplane/execution-stages/<run-key>/logs/summary.md`. Check
`.crewplane/execution-stages/<run-key>/manifests/run.json`, relevant node logs,
and `.crewplane/execution-results/<run-key>/` as needed.

## Expected Output Phrases

| Phrase | What it means | Next check |
| --- | --- | --- |
| `Mock invoker active: no provider CLI commands will be started.` | The generated mock path is active. | Inspect `.crewplane/execution-stages/<run-key>/logs/summary.md`, then `.crewplane/execution-results/<run-key>/`. |
| `CLI '<name>' not found in PATH for provider '<provider>'` | A workflow references an agent whose CLI executable is unavailable. | Confirm the command works directly or switch back to `mock`. |
| `Identical context detected` | A same-signature successful run was reused. | Use `crewplane run --force` for a fresh run. |
| `Resume advisory: would_skip` | Dry-run predicts duplicate skip. | Run with `--force` to bypass. |
| `Resume advisory: would_resume <n> node(s) from <run-id> (nodes: <ids>)` | Dry-run predicts resume hydration of the listed dependency-closed nodes from a failed or cancelled run. | Inspect `resumed_nodes` and `.crewplane/execution-stages/<run-key>/<node-id>/resume-source.json` after a run. |
| `Resuming workflow '<name>' from <n> validated node boundary(s)` | A run hydrated completed nodes from prior artifacts. | Inspect `.crewplane/execution-stages/<run-key>/manifests/run.json`. |
| `Run lock unavailable: <reason>` | The run's lock already exists or could not be created. | [Run lock unavailable](#run-lock-unavailable). |
| `tmux not found; continuing without live dashboard.` | Execution can continue without the live dashboard. | Use `--no-live` or install/configure tmux. |
| `No workflow file found` | Default discovery found no top-level `.task.md`. | Run `crewplane init` or pass `--tasks`. |
| `Multiple workflow files found` | Default discovery found more than one top-level `.task.md`. | Pass `--tasks` to select one. |

## `crewplane: command not found`

Confirm the install method finished and that the command is on `PATH`:

```bash
crewplane --help
```

For npm installs, check the npm prefix path. See
[Installation](../getting-started/installation.md).

## `No workflow file found`

Run `crewplane init`, or pass a workflow explicitly:

```bash
crewplane run --tasks .crewplane/workflows/single-agent-review.task.md
```

## `Multiple workflow files found`

Select one workflow with `--tasks` or move extra top-level `.task.md` files out
of `.crewplane/workflows/`.

## Provider Not Found During Validate

`crewplane validate` checks provider CLI availability for the built-in `cli`
invoker. Confirm the command in `agents.<name>.cli_cmd` exists on `PATH`, or use
the `mock` invoker for provider-free validation. See
[provider setup](../getting-started/provider-setup.md).

## Dry Run Differs From Validate

`run --dry-run` does not invoke providers, write run artifacts, or check provider
executable availability. It may still read existing manifests for advisory
skip/resume output.

## A Run Skipped Provider Invocation

Crewplane found a usable successful run with the same `workflow_signature`.
Inspect `.crewplane/execution-stages/<run-key>/manifests/run.json` and the
matching `.crewplane/execution-results/<run-key>/` directory. Use
`crewplane run --force` when you want a new run.

## A Run Resumed Nodes

Crewplane hydrated completed node-boundary artifacts from a failed or cancelled
run. Check `resumed_nodes` in the run manifest and
`<node-id>/resume-source.json` in resumed node stage directories. Use
`crewplane run --force` to bypass resume.

## Run Lock Unavailable

`crewplane run` creates a lock folder under `.crewplane/locks/` to prevent two
runs of the same workflow, inputs, and settings from writing files at the same
time.

`Run lock unavailable` means a lock already exists or Crewplane cannot create
one. An earlier run may still be active, or it may have stopped without removing
its lock, for example after a crash or power loss. Follow the guidance for your
platform below.

### Linux, macOS, and WSL

Rerun the command. Crewplane can remove an old lock after checking that the
previous run and its providers have stopped and that the saved run information
matches. If it cannot verify this, it stops and explains why. After removing the
lock, it marks the interrupted run as `cancelled` and can reuse saved progress
that passes its checks.

### Native Windows locks

Windows does not remove leftover locks automatically, even if a lock looks old
or the run is listed as finished. `--force` cannot bypass a lock. Follow the
manual recovery steps below.

### Manual lock recovery

If Crewplane cannot remove an old lock automatically:

1. Use your system's process manager to confirm that the original Crewplane
   command, its providers, and any programs they started have stopped. If you
   cannot confirm this, leave the lock in place.
2. Keep the saved run files for troubleshooting. Delete only the lock folder
   named in the error, then rerun your command.

Deleting a lock does not change the saved run status. If the old run is still
marked `running`, Crewplane cannot resume it. An earlier successful result may
still let Crewplane skip the run; use `--force` to run the whole workflow again.

## Native Windows launch and file errors

| Error | What to do |
| --- | --- |
| `Provider launch was withheld` with a Job Object restriction | The provider was not started. Check whether your terminal or CI runner prevents Crewplane from managing the programs it starts. |
| Sharing violation | Another program may have the file open. Close that program before retrying. |
| Path too long | Use a shorter project path. Longer paths require support from Windows, Python, and the provider tools. |

For missing provider commands or PowerShell policy errors, see
[provider setup](../getting-started/provider-setup.md#native-windows-launcher-selection).

## Template Access Denied

`{{file:path}}` is project-root bounded unless
`settings.file_access.allowed_template_paths` includes an
absolute allowlisted path. Symlinks are resolved before the final access check.

## Quota Or Rate Limit

Start with the node log that captured provider output and copy the exact quota
or rate-limit phrase. Then configure provider-specific quota detection under
`agents.<name>`:

```yaml
quota_reached_on_contains:
  - "rate limit reached"
quota_reached_retry_delay_seconds: 300
quota_reset_sleep_floor_seconds: 5
```

## tmux Missing

If the `tmux` executable cannot be found, Crewplane warns and continues without
the live dashboard. Install tmux, set
`settings.integrations.ui.options.tmux_executable`, use
`settings.integrations.ui.implementation: "none"`, or pass `--no-live`.

## No Live Dashboard In CI

The live dashboard only starts for non-dry runs attached to a terminal. CI and
other non-TTY runs still write `.crewplane/execution-stages/<run-key>/logs/`.

## Mock File Mode Did Not Find My Fixture

Mock file mode searches from node/task/round-specific fixtures down to
`default-<role>.md` and `default.md`. Use `strict_file_mode: true` when you want
missing fixtures to fail instead of falling back to generated mock output.

## Workspace Unsupported Repository

Workspace isolation requires an ordinary Git repository compatible
with the `blob_exact` source contract. Disable workspace support for non-Git
projects, Git LFS, custom filters, text/eol conversions, submodules, sparse
clone, or partial clone unless support has been verified locally.

## Cleanup Requires Git Scope

`crewplane cleanup workspaces` is scoped to the current Git repository by
default. Use `--all-projects` to clean every repository bucket under the
workspace cache root.

## Workspace Node Did Not Produce A Bundle Or Branch

Start with `.crewplane/execution-stages/<run-key>/manifests/run.json` and the
relevant node logs. Confirm the node status, worktree lineage, `create_branch`,
and final lineage checkpoint. Only successful `kind: worktree` lineage nodes
produce bundles. `snapshot` nodes, `worktree: none` nodes, failed nodes, and
nodes with invalid final Git state do not. Branch export also requires
`create_branch: true` and a verified final lineage checkpoint.

## Cleanup Found Zero Paths

Cleanup is scoped to the current Git repository by default. Check that the
workflow used workspace isolation, confirm the configured cache root, and use
`--all-projects` only when you intentionally want every repository bucket under
that cache root.

## Next

Continue to [Reproducible Support Bundle](reproducible-support-bundle.md) to
collect a redacted set of files when someone else needs to inspect a run.

Or return to the [Guides](../index.md#guided-tutorial-track).
