# Quickstart

Run these commands from the project you want provider CLIs to inspect or modify:

```bash
crewplane init
crewplane validate
crewplane run --dry-run
crewplane run
```

After `crewplane init`, choose one first-run path:

- Use real providers: install and authenticate the CLIs referenced in
  `.crewplane/config.yml`, then edit the workflow providers if you only want a
  subset.
- Use mock execution: switch `settings.integrations.invoker.implementation` to
  `mock` before `crewplane validate`.

Before a real run, review `.crewplane/config.yml`. The generated provider
examples may include provider-specific unattended approval or sandbox-bypass
flags so the default templates can run without interactive prompts.

## Initialize

`crewplane init` creates project-local Crewplane files under `.crewplane/`:

- `.crewplane/config.yml`
- `.crewplane/workflows/code-review-example.task.md`
- `.crewplane/workflows/example-templates/**`
- `.crewplane/workflows/example-templates/sample-inputs/*.md`
- `.crewplane/preflight/fingerprint.key`, when a key can be persisted

Generated config and workflow schema values come from the current
`SCHEMA_VERSION` in `src/crewplane/version.py`.

## Validate

```bash
crewplane validate
crewplane validate .crewplane/workflows/code-review-example.task.md
crewplane validate .crewplane/workflows/code-review-example.task.md --config .crewplane/config.yml
```

`validate` parses and composes the workflow, validates providers and policies,
compiles a preflight execution-plan preview, and checks configured provider CLI
availability for the built-in `cli` invoker. It does not invoke providers and
does not write run artifacts.

## Dry Run

```bash
crewplane run --dry-run
crewplane run -n --tasks .crewplane/workflows/code-review-example.task.md
```

`run --dry-run` compiles and prints the execution plan without invoking
providers, writing run artifacts, or checking provider executable availability.
With the filesystem artifact backend, it may read existing manifests to print a
non-binding skip or resume advisory.

## Real Run

```bash
crewplane run
crewplane run --no-live
crewplane run --force
```

A real run compiles preflight, writes a run directory under
`.crewplane/execution-stages/`, invokes providers, writes final node artifacts
under `.crewplane/execution-results/`, and records manifests and logs.

Use `--no-live` to disable the live dashboard while keeping execution fully
functional. Use `--force` to bypass duplicate-skip and resume behavior for the
same workflow signature.

## Selecting A Workflow

By default, `crewplane run` and `crewplane validate` expect exactly one
`.task.md` file directly in `.crewplane/workflows/`. If there are zero or
multiple top-level workflow files, pass one explicitly:

```bash
crewplane run --tasks .crewplane/workflows/code-review-example.task.md
crewplane run -t .crewplane/workflows/example-templates/feature-implement-example.task.md
```

The example library under `.crewplane/workflows/example-templates/` is not
selected by default unless you pass `--tasks`.
