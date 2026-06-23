# Crewplane Documentation

Crewplane is a local workflow runner for AI coding CLIs. You describe the work
in a Markdown `.task.md` file, Crewplane runs the configured provider commands,
and each run leaves readable artifacts under `.crewplane/`.

## What Crewplane Provides

- Markdown workflow DAGs with parallel, sequential, and file-backed input nodes.
- CLI-first provider execution through configured external commands; Crewplane
  does not install or authenticate those provider tools.
- Auditable filesystem artifacts under `.crewplane/execution-stages/` and
  `.crewplane/execution-results/`.
- Workflow composition with imports, aliases, parameters, and input binding.
- Preflight compilation, idempotency, and filesystem-backed resume.
- Provider-free validation through the deterministic `mock` invoker.
- Optional tmux live dashboard with graceful fallback.
- Opt-in Experimental Git-backed source-tree isolation, not sandboxing.
- Replaceable invoker, UI, and artifact adapters.

## Getting Started

- [Installation](getting-started/installation.md)
- [Quickstart](getting-started/quickstart.md)
- [Provider setup](getting-started/provider-setup.md)

## Concepts

- [Orchestration model](concepts/orchestration-model.md)
- [Workflows](concepts/workflows.md)
- [Preflight and idempotency](concepts/preflight-and-idempotency.md)

## Guides

- [Running workflows](guides/running-workflows.md)
- [Workflow composition](guides/workflow-composition.md)
- [Findings and review loops](guides/findings-and-review-loops.md)
- [Mock validation](guides/mock-validation.md)
- [Observability](guides/observability.md)
- [Experimental workspace isolation](guides/workspace-isolation.md)
- [Experimental worktree implementation](guides/experimental-worktree-implementation/index.md)
- [Inspecting artifacts](guides/inspecting-artifacts.md)
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

## Architecture And Contributors

- [Architecture index](architecture/index.md)
- [Development guide](../DEVELOPMENT.md)
- [Agent instructions](../AGENTS.md)

## Maintainer Notes

Documents under `docs/maintainers/` are maintainer-facing records and design
notes. Some preserve historical context from before the clean-break rename to
Crewplane. For current user behavior, start with the guides and reference pages
listed above.
