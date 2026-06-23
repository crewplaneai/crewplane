# Troubleshooting

## `No workflow file found`

Run `orchestrator init`, or pass a workflow explicitly:

```bash
orchestrator run --tasks .orchestrator/workflows/code-review-example.task.md
```

## `Multiple workflow files found`

Select one workflow with `--tasks` or move extra top-level `.task.md` files out
of `.orchestrator/workflows/`.

## Provider Not Found During Validate

`orchestrator validate` checks provider CLI availability for the built-in `cli`
invoker. Confirm the command in `agents.<name>.cli_cmd` exists on `PATH`, or use
the `mock` invoker for provider-free validation.

## Dry Run Differs From Validate

`run --dry-run` does not invoke providers, write run artifacts, or check provider
executable availability. It may still read existing manifests for advisory
skip/resume output.

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

## Experimental Workspace Unsupported Repository

Experimental workspace isolation requires an ordinary Git repository compatible
with the `blob_exact` source contract. Disable workspace support for non-Git
projects, Git LFS, custom filters, text/eol conversions, submodules, sparse
clone, or partial clone unless support has been verified locally.

## Cleanup Requires Git Scope

`orchestrator cleanup workspaces` is scoped to the current Git repository by
default. Use `--all-projects` to clean every repository bucket under the
workspace cache root.
