# Running Workflows

## Default Discovery

`crewplane validate` and `crewplane run` look for exactly one top-level
`.task.md` file in `.crewplane/workflows/` when no workflow path is supplied.
Fresh projects contain:

```text
.crewplane/workflows/single-agent-review.task.md
```

Advanced examples are copied under `.crewplane/workflows/example-templates/`.
They are not selected by default.

If there are zero or multiple top-level workflow files, select one explicitly:

```bash
crewplane validate .crewplane/workflows/single-agent-review.task.md
crewplane run --tasks .crewplane/workflows/single-agent-review.task.md
```

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
shape, template references, and preflight plan compilation. For the built-in
`cli` invoker, it also checks configured provider CLI availability and points
failures to [provider setup](../getting-started/provider-setup.md). With the
generated mock config, it invokes no providers and checks no provider CLIs.

## Dry Run

```bash
crewplane run --dry-run --tasks .crewplane/workflows/single-agent-review.task.md
```

Dry-run prints the compiled execution plan and, with the filesystem artifact
backend, an advisory skip/resume decision based on existing manifests. It
invokes no providers, writes no run artifacts, and skips provider CLI
availability checks.

## Real Execution

```bash
crewplane run --no-live
crewplane run --tasks .crewplane/workflows/single-agent-review.task.md --no-live
```

Real execution writes preflight/run artifacts, invokes the configured invoker,
records logs and manifests, and writes consolidated results.

Use:

- `--no-live` to disable live dashboard output.
- `--tasks` or `-t` to select a workflow.
- `--config` or `-c` to select config.
- `--force` to bypass same-signature skip and failed/cancelled-run resume.

## Duplicate Skip

Crewplane computes a `workflow_signature` from the compiled workflow context and
runtime settings. When a previous successful run with the same signature is
usable, a later run can skip provider invocation and reuse the recorded result.

Evidence lives under:

- `.crewplane/execution-stages/<run-key>/manifests/run.json`
- `.crewplane/execution-results/<run-key>/`

Use `crewplane run --dry-run` to preview the advisory decision. Use `--force`
when you intentionally want a new run even if an identical success exists.

## Resume

When a previous run failed or was cancelled after some nodes completed,
Crewplane can resume from validated node boundaries. The new run gets its own
stage/result directories and records which nodes were resumed.

Look for:

- `resumed_node_ids` in `.crewplane/execution-stages/<run-key>/manifests/run.json`
- `<node-id>/resume-source.json` in resumed node stage directories
- hydrated result files under `.crewplane/execution-results/<run-key>/`

Use `--force` to bypass resume hydration and rerun every selected node.

## Artifact Backend

The built-in filesystem artifact backend is the supported backend for normal
real runs. A custom artifact backend must implement the same port capabilities
for locks, skip/resume history, full-run output, and workspace lineage before it
can replace it.
