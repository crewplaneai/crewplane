# Command Reference

Use this page for exact CLI syntax. For task-oriented guidance, start with
[Running workflows](../guides/running-workflows.md).

| Command | Use when |
| --- | --- |
| `crewplane init` | Create project-local config and example workflows. |
| `crewplane validate` | Check workflow/config validity without invoking providers. |
| `crewplane run` | Execute, dry-run, or force a workflow run. |
| `crewplane cleanup workspaces` | Remove Experimental workspace cache entries. |

## `crewplane init`

Initialize project-local config and example workflows.

```bash
crewplane init
```

Creates:

- `.crewplane/config.yml`
- `.crewplane/workflows/single-agent-review.task.md`
- `.crewplane/workflows/example-templates/**`
- `.crewplane/workflows/example-templates/sample-inputs/*.md`
- `.crewplane/preflight/fingerprint.key`, when possible

The generated config uses deterministic mock execution.

Existing files are not overwritten by template creation.

## `crewplane validate`

Validate a workflow definition.

```bash
crewplane validate [TASKS_FILE] --config .crewplane/config.yml
crewplane validate [TASKS_FILE] -c .crewplane/config.yml
```

Arguments and options:

| Name | Description |
| --- | --- |
| `TASKS_FILE` | Workflow file to validate. Defaults to a single top-level `.crewplane/workflows/*.task.md`. |
| `--config`, `-c` | Config file. Defaults to `.crewplane/config.yml`. |

`validate` invokes no providers and writes no run artifacts. For the built-in
`cli` invoker, it checks configured provider CLI availability.

## `crewplane run`

Execute a workflow DAG.

```bash
crewplane run --no-live
crewplane run --tasks .crewplane/workflows/single-agent-review.task.md
crewplane run -t .crewplane/workflows/single-agent-review.task.md
```

Options:

| Name | Description |
| --- | --- |
| `--tasks`, `-t` | Workflow file. Defaults to a single top-level `.crewplane/workflows/*.task.md`. |
| `--config`, `-c` | Config file. Defaults to `.crewplane/config.yml`. |
| `--dry-run`, `-n` | Show the execution plan without invoking providers or writing run artifacts. |
| `--force` | Run fresh and intentionally bypass both duplicate skip and resume hydration. |
| `--no-live` | Disable live topology dashboard output. |

When the mock invoker is active, `run` prints that no provider CLI commands will
be started. `run --dry-run` skips provider executable availability checks and
may read existing manifests for an advisory skip/resume message.

Use `--force` when you want a fresh run and intentionally want to bypass both
duplicate skip and resume hydration.

## `crewplane cleanup workspaces`

Remove generated Experimental workspace isolation cache entries.

```bash
crewplane cleanup workspaces --dry-run
crewplane cleanup workspaces --yes
```

The command is advisory by default. Destructive cleanup happens only when
`--yes` is set and `--dry-run` is not set.

See [Cleaning Up Workspace Caches](../guides/cleanup.md) for the operational
guide and run-record retention note.

Options:

| Name | Description |
| --- | --- |
| `--config`, `-c` | Config file. Defaults to `.crewplane/config.yml`. |
| `--dry-run` | Show workspaces that would be removed. |
| `--run` | Only clean workspaces for this run key. |
| `--older-than` | Only clean entries older than a duration such as `30m`, `12h`, or `7d`. |
| `--yes` | Confirm destructive workspace cleanup. |
| `--successful` | Only clean succeeded workspace states. |
| `--failed` | Only clean failed workspace states. |
| `--cancelled` | Only clean cancelled workspace states. |
| `--orphans` | Only clean cache paths without workspace state. |
| `--all-projects` | Clean every repository bucket under the workspace cache. |

By default, cleanup is scoped to the current Git repository. Non-Git projects
must use `--all-projects`. `--all-projects` cannot be combined with
`--orphans`, `--successful`, `--failed`, or `--cancelled` because those filters
depend on current-project workspace-state artifacts.

Cleanup rejects workspace cache roots that are relative, symlinks, overlap the
project, overlap `.crewplane/`, overlap run artifact directories, or overlap Git
metadata paths.
