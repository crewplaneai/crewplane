# Crewplane Documentation

Crewplane turns coding-agent CLI calls into explicit, resumable workflows with
local run records on disk.

## Start By Goal

| Goal | Start here |
| --- | --- |
| Understand what Crewplane is | [Why Crewplane?](concepts/why-crewplane.md) and [Orchestration model](concepts/orchestration-model.md) |
| Try it safely without real agent calls | [Quickstart](getting-started/quickstart.md), then [First Run Checklist](getting-started/setup-checklist.md) |
| Connect, run, author, or debug workflows | [Provider setup](getting-started/provider-setup.md), [Running workflows](guides/running-workflows.md), [Workflows](concepts/workflows.md), [Troubleshooting](safety/troubleshooting.md) |
| Look up exact syntax and config | [Workflow syntax](reference/workflow-syntax.md), [Configuration](reference/configuration.md), [Commands](reference/commands.md) |
| Understand security boundaries | [Security and trust](safety/security-and-trust.md) |

## Common Paths

### I Am Evaluating Crewplane

1. Read [Why Crewplane?](concepts/why-crewplane.md).
2. Run the provider-free [Quickstart](getting-started/quickstart.md).
3. Open the run summary and final result.

### I Already Have A Provider CLI Installed

1. Run the mock quickstart first.
2. Follow [Provider setup](getting-started/provider-setup.md).
3. Run one real workflow with `--no-live`.
4. Inspect the run record.

### I Need To Debug A Run

1. Check [Running workflows](guides/running-workflows.md).
2. Open [Inspecting Run Records](guides/inspecting-artifacts.md).
3. Use [Troubleshooting](safety/troubleshooting.md).

## Getting Started

- [Installation](getting-started/installation.md)
- [Quickstart](getting-started/quickstart.md)
- [First Run Checklist](getting-started/setup-checklist.md)
- [Provider setup](getting-started/provider-setup.md)

## Core Concepts

- [Why Crewplane?](concepts/why-crewplane.md)
- [Orchestration model](concepts/orchestration-model.md)
- [Workflows](concepts/workflows.md)
- [Preflight, duplicate skip, and resume](concepts/preflight-and-idempotency.md)

## Guides

- [Running workflows](guides/running-workflows.md)
- [Inspecting Run Records](guides/inspecting-artifacts.md)
- [Mock validation](guides/mock-validation.md)
- [Observability](guides/observability.md)
- [Workflow composition](guides/workflow-composition.md)
- [Node modes and provider roles](guides/node-modes.md)
- [Findings and review loops](guides/findings-and-review-loops.md)
- [Cleanup](guides/cleanup.md)

## Examples

- [Example templates](examples/index.md)
- [Composition examples](examples/composition.md)
- [Experimental workspace examples](examples/workspace.md)

## Reference

- [Commands](reference/commands.md)
- [Configuration](reference/configuration.md)
- [Workflow syntax](reference/workflow-syntax.md)
- [Integrations](reference/integrations.md)
- [Artifacts](reference/artifacts.md)

## Safety And Troubleshooting

- [Security and trust](safety/security-and-trust.md)
- [Troubleshooting](safety/troubleshooting.md)
- [Reproducible support bundle](safety/reproducible-support-bundle.md)

## Advanced

- [Experimental source-tree isolation, not sandboxing](guides/workspace-isolation.md)
