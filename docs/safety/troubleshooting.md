# Troubleshooting

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
run. Check `resumed_node_ids` in the run manifest and
`<node-id>/resume-source.json` in resumed node stage directories. Use
`crewplane run --force` to bypass resume.

## Template Access Denied

`{{file:path}}` is project-root bounded unless
`settings.integrations.artifacts.options.allowed_template_paths` includes an
absolute allowlisted path. Symlinks are resolved before the final access check.

## Quota Or Rate Limit

Configure provider-specific quota detection under `agents.<name>`:

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

The live dashboard only starts for real runs attached to a terminal. CI and
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

Only successful `kind: worktree` lineage nodes produce bundles. `snapshot`
nodes, `worktree: none` nodes, failed nodes, and nodes with invalid final Git
state do not. Branch export also requires `create_branch: true` and a verified
final lineage checkpoint.

## Cleanup Found Zero Paths

Cleanup is scoped to the current Git repository by default. Check that the
workflow used workspace isolation, confirm the configured cache root, and use
`--all-projects` only when you intentionally want every repository bucket under
that cache root.

## Need A Support Bundle

Use [reproducible support bundle](reproducible-support-bundle.md) to collect
command output, config, workflow, run summary, events, relevant node files,
versions, platform details, and redacted provider logs.
