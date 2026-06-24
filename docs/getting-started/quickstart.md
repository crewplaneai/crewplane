# Quickstart

Run these commands from the project where you want Crewplane to create local
workflow artifacts:

```bash
crewplane init
crewplane validate
crewplane run --no-live
```

This first run uses deterministic mock execution. It does not require provider
CLIs, API keys, provider accounts, or config edits. Mock output is scaffolding
for validating Crewplane behavior and artifact paths; it is not model output.

## Initialize

`crewplane init` creates project-local files under `.crewplane/`:

- `.crewplane/config.yml`
- `.crewplane/workflows/single-agent-review.task.md`
- `.crewplane/workflows/example-templates/**`
- `.crewplane/workflows/example-templates/sample-inputs/*.md`
- `.crewplane/preflight/fingerprint.key`, when a key can be persisted

The generated config has one active agent, `mock`, and uses
`settings.integrations.invoker.implementation: "mock"`.

## Validate

```bash
crewplane validate
```

`validate` parses and composes the default workflow, validates providers and
policies, and compiles a preflight execution-plan preview. With the generated
mock config it does not check or start provider CLIs.

## Run

```bash
crewplane run --no-live
```

The run writes normal Crewplane artifacts while using deterministic mock output.
Use `--no-live` for the quickstart so the result is a simple terminal run even
when tmux is installed.

Inspect the durable run files next:

```bash
find .crewplane/execution-stages -maxdepth 4 -type f | sort
find .crewplane/execution-results -maxdepth 3 -type f | sort
```

Start with:

- `.crewplane/execution-stages/<run-key>/logs/summary.md`
- `.crewplane/execution-stages/<run-key>/logs/events.ndjson`
- `.crewplane/execution-results/<run-key>/review.project-result.md`

## Setup Checklist

Use the [setup checklist](setup-checklist.md) to confirm mock status, safety
status, artifact status, live UI status, and readiness for real providers.

## Real Providers

After the mock run succeeds and you have inspected artifacts, configure real
provider CLIs with [provider setup](provider-setup.md). Real provider runs start
the external commands configured in `.crewplane/config.yml`.

## Selecting A Workflow

By default, `crewplane run` and `crewplane validate` expect exactly one
`.task.md` file directly in `.crewplane/workflows/`.

Advanced examples are copied under `.crewplane/workflows/example-templates/` and
are not selected by default. Run them explicitly after provider setup or after
adjusting their provider names:

```bash
crewplane validate .crewplane/workflows/example-templates/code-review-example.task.md
crewplane run --tasks .crewplane/workflows/example-templates/code-review-example.task.md
```
