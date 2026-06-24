# Crewplane Documentation

Crewplane gives a project a repeatable workflow path for AI-assisted work, with
readable run artifacts stored under `.crewplane/`. The default project created
by `crewplane init` uses deterministic mock execution, so you can validate the
workflow and inspect artifacts before installing or authenticating provider
CLIs.

## Getting Started

- [Installation](getting-started/installation.md)
- [Quickstart](getting-started/quickstart.md)
- [Setup checklist](getting-started/setup-checklist.md)
- [Provider setup](getting-started/provider-setup.md)

## Core Concepts

- [Orchestration model](concepts/orchestration-model.md)
- [Workflows](concepts/workflows.md)
- [Preflight and idempotency](concepts/preflight-and-idempotency.md)

## Guides

- [Running workflows](guides/running-workflows.md)
- [Inspecting artifacts](guides/inspecting-artifacts.md)
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
