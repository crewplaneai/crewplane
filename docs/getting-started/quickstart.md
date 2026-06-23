# Quickstart

Run these commands from the project you want providers to inspect or modify:

```bash
orchestrator init
orchestrator validate
orchestrator run --dry-run
orchestrator run
```

## Initialize

`orchestrator init` creates project-local files under `.orchestrator/`:

- `.orchestrator/config.yml`
- `.orchestrator/workflows/code-review-example.task.md`
- `.orchestrator/workflows/example-templates/**`
- `.orchestrator/workflows/example-templates/sample-inputs/*.md`
- `.orchestrator/preflight/fingerprint.key`, when a key can be persisted

Generated config and workflow schema values are rendered from the current
`src/orchestrator_cli/version.py` `SCHEMA_VERSION`.

## Validate

```bash
orchestrator validate
orchestrator validate .orchestrator/workflows/code-review-example.task.md
orchestrator validate .orchestrator/workflows/code-review-example.task.md --config .orchestrator/config.yml
```

`validate` parses and composes the workflow, validates providers and policies,
compiles a preflight execution-plan preview, and checks configured provider CLI
availability for the built-in `cli` invoker. It does not invoke providers and
does not write run artifacts.

## Dry Run

```bash
orchestrator run --dry-run
orchestrator run -n --tasks .orchestrator/workflows/code-review-example.task.md
```

`run --dry-run` compiles and prints the execution plan without invoking
providers, writing run artifacts, or checking provider executable availability.
With the filesystem artifact backend, it may read existing manifests to print a
non-binding skip or resume advisory.

## Real Run

```bash
orchestrator run
orchestrator run --no-live
orchestrator run --force
```

A real run compiles preflight, writes a run directory under
`.orchestrator/execution-stages/`, invokes providers, writes final node artifacts
under `.orchestrator/execution-results/`, and records manifests and logs.

Use `--no-live` to disable the live dashboard while keeping execution fully
functional. Use `--force` to bypass duplicate-skip and resume behavior for the
same workflow signature.

## Selecting A Workflow

By default, `orchestrator run` and `orchestrator validate` expect exactly one
`.task.md` file directly in `.orchestrator/workflows/`. If there are zero or
multiple top-level workflow files, pass one explicitly:

```bash
orchestrator run --tasks .orchestrator/workflows/code-review-example.task.md
orchestrator run -t .orchestrator/workflows/example-templates/feature-implement-example.task.md
```

The example library under `.orchestrator/workflows/example-templates/` is not
selected by default unless you pass `--tasks`.
