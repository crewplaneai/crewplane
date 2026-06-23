# Command Reference

## `orchestrator init`

Initialize project-local config and example workflows.

```bash
orchestrator init
```

Creates:

- `.orchestrator/config.yml`
- `.orchestrator/workflows/code-review-example.task.md`
- `.orchestrator/workflows/example-templates/**`
- `.orchestrator/workflows/example-templates/sample-inputs/*.md`
- `.orchestrator/preflight/fingerprint.key`, when possible

Existing files are not overwritten by template creation.

## `orchestrator validate`

Validate a workflow definition.

```bash
orchestrator validate [TASKS_FILE] --config .orchestrator/config.yml
orchestrator validate [TASKS_FILE] -c .orchestrator/config.yml
```

Arguments and options:

| Name | Description |
| --- | --- |
| `TASKS_FILE` | Workflow file to validate. Defaults to a single top-level `.orchestrator/workflows/*.task.md`. |
| `--config`, `-c` | Config file. Defaults to `.orchestrator/config.yml`. |

`validate` invokes no providers and writes no run artifacts. For the built-in
`cli` invoker, it checks configured provider CLI availability.

## `orchestrator run`

Execute a workflow DAG.

```bash
orchestrator run --tasks .orchestrator/workflows/code-review-example.task.md
orchestrator run -t .orchestrator/workflows/code-review-example.task.md
```

Options:

| Name | Description |
| --- | --- |
| `--tasks`, `-t` | Workflow file. Defaults to a single top-level `.orchestrator/workflows/*.task.md`. |
| `--config`, `-c` | Config file. Defaults to `.orchestrator/config.yml`. |
| `--dry-run`, `-n` | Show the execution plan without invoking providers or writing run artifacts. |
| `--force` | Run even if a matching successful `workflow_signature` exists; also bypasses resume hydration. |
| `--no-live` | Disable live topology dashboard output. |

`run --dry-run` skips provider executable availability checks and may read
existing manifests for an advisory skip/resume message.

## `orchestrator cleanup workspaces`

Remove generated Experimental workspace isolation cache entries.

```bash
orchestrator cleanup workspaces --dry-run
orchestrator cleanup workspaces --yes
```

Options:

| Name | Description |
| --- | --- |
| `--config`, `-c` | Config file. Defaults to `.orchestrator/config.yml`. |
| `--dry-run` | Show workspaces that would be removed. |
| `--run` | Only clean workspaces for this run key. |
| `--older-than` | Only clean entries older than a duration such as `30m`, `12h`, or `7d`. |
| `--yes` | Confirm destructive workspace cleanup. |
| `--successful` | Only clean succeeded workspace states. |
| `--failed` | Only clean failed workspace states. |
| `--cancelled` | Only clean cancelled workspace states. |
| `--orphans` | Only clean cache paths without workspace state. |
| `--all-projects` | Clean every repository bucket under the workspace cache. |
