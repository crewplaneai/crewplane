# Running Workflows

## Default Discovery

`crewplane validate` and `crewplane run` look for exactly one top-level
`.task.md` file in `.crewplane/workflows/` when no workflow path is supplied.

If there are zero or multiple top-level workflow files, select one explicitly:

```bash
crewplane validate .crewplane/workflows/code-review-example.task.md
crewplane run --tasks .crewplane/workflows/code-review-example.task.md
```

Nested example templates are available after `crewplane init`, but they are
not selected by default.

## Config Selection

By default, commands use `.crewplane/config.yml`. Override it with:

```bash
crewplane validate --config .crewplane/config.yml
crewplane run --config .crewplane/config.yml
```

## Validate

```bash
crewplane validate [TASKS_FILE] --config .crewplane/config.yml
```

`validate` checks workflow parsing, composition, schema, providers, policies, DAG
shape, template references, preflight plan compilation, and built-in `cli`
provider executable availability. It invokes no providers and writes no run
artifacts.

## Dry Run

```bash
crewplane run --dry-run --tasks .crewplane/workflows/code-review-example.task.md
```

Dry-run prints the compiled execution plan and, with the filesystem artifact
backend, an advisory skip/resume decision based on existing manifests. It invokes
no providers, writes no run artifacts, and skips provider CLI availability
checks.

## Real Execution

```bash
crewplane run --tasks .crewplane/workflows/code-review-example.task.md
```

Real execution writes preflight/run artifacts, invokes providers, records logs
and manifests, and writes consolidated results.

Use:

- `--force` to bypass same-signature skip and resume hydration.
- `--no-live` to disable live dashboard output.
- `--tasks` or `-t` to select a workflow.
- `--config` or `-c` to select config.

The built-in filesystem artifact backend is the supported backend for normal
real runs. A custom artifact backend must implement the same port capabilities
for locks, skip/resume history, full-run output, and workspace lineage before it
can replace it.
