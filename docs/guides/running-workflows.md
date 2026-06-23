# Running Workflows

## Default Discovery

`orchestrator validate` and `orchestrator run` look for exactly one top-level
`.task.md` file in `.orchestrator/workflows/` when no workflow path is supplied.

If there are zero or multiple top-level workflow files, select one explicitly:

```bash
orchestrator validate .orchestrator/workflows/code-review-example.task.md
orchestrator run --tasks .orchestrator/workflows/code-review-example.task.md
```

Nested example templates are available after `orchestrator init`, but they are
not selected by default.

## Config Selection

By default, commands use `.orchestrator/config.yml`. Override it with:

```bash
orchestrator validate --config .orchestrator/config.yml
orchestrator run --config .orchestrator/config.yml
```

## Validate

```bash
orchestrator validate [TASKS_FILE] --config .orchestrator/config.yml
```

`validate` checks workflow parsing, composition, schema, providers, policies, DAG
shape, template references, preflight plan compilation, and built-in `cli`
provider executable availability. It invokes no providers and writes no run
artifacts.

## Dry Run

```bash
orchestrator run --dry-run --tasks .orchestrator/workflows/code-review-example.task.md
```

Dry-run prints the compiled execution plan and, with the filesystem artifact
backend, an advisory skip/resume decision based on existing manifests. It invokes
no providers, writes no run artifacts, and skips provider CLI availability
checks.

## Real Execution

```bash
orchestrator run --tasks .orchestrator/workflows/code-review-example.task.md
```

Real execution writes preflight/run artifacts, invokes providers, records logs
and manifests, and writes consolidated results.

Use:

- `--force` to bypass same-signature skip and resume hydration.
- `--no-live` to disable live dashboard output.
- `--tasks` or `-t` to select a workflow.
- `--config` or `-c` to select config.

Real execution currently requires the built-in filesystem artifact backend for
lock, skip, resume, full-run, and workspace-lineage behavior.
