# Troubleshooting

## Start By Symptom

| Symptom | Start here |
| --- | --- |
| Command not found | [Installation](../getting-started/installation.md). |
| No workflow found | [Default discovery](running-workflows.md#default-discovery). |
| Provider not found | [Provider setup](../getting-started/provider-setup.md). |
| Run skipped | [Duplicate skip](running-workflows.md#duplicate-skip). |
| Run resumed | [Resume](running-workflows.md#resume). |
| Run lock unavailable | [Run lock unavailable](#run-lock-unavailable). |
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
| `Resume advisory: would_resume <n> node(s) from <run-id>` | Dry-run predicts resume hydration from a failed or cancelled run. | Inspect `resumed_nodes` and `.crewplane/execution-stages/<run-key>/<node-id>/resume-source.json` after a run. |
| `Resuming workflow '<name>' from <n> validated node boundary(s)` | A run hydrated completed nodes from prior artifacts. | Inspect `.crewplane/execution-stages/<run-key>/manifests/run.json`. |
| `Run lock unavailable: <reason>` | The same-context run lock could not be acquired. | [Run lock unavailable](#run-lock-unavailable). |
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

`crewplane run` holds a lock directory under `.crewplane/locks/` while a
workflow executes, so two runs of the same workflow context cannot interleave.
The lock records its owning process and is released when the run ends.

`Run lock unavailable` means the lock could not be acquired. Either a matching
run is still active, or a previous run ended without releasing the lock, for
example after a crash or power loss. When the recorded owner process is no
longer alive, the next run reclaims the lock automatically and finalizes the
interrupted run's manifest as `cancelled`, which makes it a valid resume
source.

If the message repeats with no active run, or an error traceback references
`.crewplane/locks/`, confirm no crewplane process is running, then delete the
lock state and retry:

```bash
rm -rf .crewplane/locks
```

Manual deletion skips the automatic finalization, so the interrupted run keeps
`status: running` and is not considered for resume. If an older successful run
with the same context exists, the next run prints `Identical context detected`;
use `--force` for a fresh run.

## Template Access Denied

`{{file:path}}` is project-root bounded unless
`settings.integrations.artifacts.options.allowed_template_paths` includes an
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

## Experimental Workspace Unsupported Repository

Experimental workspace isolation requires an ordinary Git repository compatible
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

Or return to the [Guides](../index.md#guides).
