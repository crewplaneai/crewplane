# Quickstart: See the Control Plane

This quickstart does not call a model. It uses deterministic mock execution so
you can see Crewplane's workflow machinery without provider CLIs, credentials,
API keys, provider accounts, token cost, config edits, or `tmux`.

You will see Crewplane:

1. create a project-local workflow
2. validate and compile a preflight execution plan
3. execute the DAG with the mock provider
4. write a local run record with logs, manifests, stage outputs, and results

Run these commands from the project where you want Crewplane to create local
workflow files and run records:

```bash
crewplane init
crewplane validate
crewplane run --no-live
```

Mock output is scaffolding for validating Crewplane behavior and run-record
paths. It is not model output.

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

The run writes the normal Crewplane run record while using deterministic mock
output.
Use `--no-live` for the quickstart so the result is a simple terminal run even
when tmux is installed.

## Inspect The Run Record

The mock output is not the point. The point is the shape of the run record.

Start with:

- `.crewplane/execution-stages/<run-key>/logs/summary.md`
- `.crewplane/execution-stages/<run-key>/logs/events.ndjson`
- `.crewplane/execution-results/<run-key>/review.project-result.md`

Then list everything that was written:

```bash
find .crewplane/execution-stages -maxdepth 4 -type f | sort
find .crewplane/execution-results -maxdepth 3 -type f | sort
```

## After The Mock Run

Want to see the same flow with a real provider? Watch the Codex walkthrough:

<div align="center">
  <video src="https://github.com/user-attachments/assets/01ff3e39-7626-4896-bd18-358f7a15cfcd" controls width="80%" title="First real run demo with Codex"></video>
</div>

## First Run Checklist

Use the [First Run Checklist](setup-checklist.md) to confirm mock status, safety
status, run-record status, live UI status, and readiness for real providers.

## Real Providers

After the mock run succeeds and you have inspected the run record, configure
real provider CLIs with [provider setup](provider-setup.md). Real provider runs
start the external commands configured in `.crewplane/config.yml`.

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

## Next

Use the [First Run Checklist](setup-checklist.md). If every section passes,
continue to [Provider setup](provider-setup.md).
