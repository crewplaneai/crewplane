# Crewplane

[![Status: alpha](https://img.shields.io/badge/status-alpha-f59e0b.svg)](https://github.com/crewplaneai/crewplane/blob/main/CHANGELOG.md)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-3776AB.svg)](https://github.com/crewplaneai/crewplane/blob/main/pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/crewplaneai/crewplane/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-read-0f766e.svg)](https://github.com/crewplaneai/crewplane/blob/main/docs/index.md)
[![Security policy](https://img.shields.io/badge/security-policy-b91c1c.svg)](https://github.com/crewplaneai/crewplane/blob/main/SECURITY.md)
[![Contributing](https://img.shields.io/badge/contributing-guide-6b7280.svg)](https://github.com/crewplaneai/crewplane/blob/main/CONTRIBUTING.md)

[Documentation](https://github.com/crewplaneai/crewplane/blob/main/docs/index.md) · [Changelog](https://github.com/crewplaneai/crewplane/blob/main/CHANGELOG.md) · [Contributing](https://github.com/crewplaneai/crewplane/blob/main/CONTRIBUTING.md) · [Security](https://github.com/crewplaneai/crewplane/blob/main/SECURITY.md)

Crewplane runs AI coding CLIs through auditable Markdown workflows.

A `.task.md` file is both the workflow map and the instructions: the YAML
frontmatter lists the steps, providers, and dependencies, while each Markdown
section gives a step its detailed prompt. Assign steps to providers such as
Claude, Codex, Gemini, Copilot, Kilo, or any generic CLI, and let
`crewplane` run the graph while preserving inputs, intermediate outputs,
logs, manifests, and final results under `.crewplane/`.

Crewplane is built around four ideas:

- Workflows are Markdown files with explicit dependencies.
- Providers are external CLIs, not vendor SDK integrations.
- Execution state is written to disk as readable artifacts.
- Invoker, UI, and artifact integrations are modular, so teams can swap built-ins or provide their own adapters.

> *"Crewplane doesn't try to understand your AI tools. It just gives them a shared workspace and gets out of the way."*

## Why Crewplane?

You already have AI coding tools you love. But they can't talk to each other. Claude doesn't know what Codex just wrote. Gemini can't review Claude's output.

**Crewplane connects them.** Define tasks in a Markdown file, assign each to an AI provider, and Crewplane runs them — in parallel when possible, in sequence when needed. Each tool reads and writes to a shared folder, so downstream tasks can build on upstream results.

No SDKs. No plugins. No vendor lock-in. If your AI tool has a CLI, it works.

**Every step is a Markdown file.** Inputs, outputs, reviews — all saved to `.crewplane/execution-stages/` as readable Markdown. Inspect any step in your editor, diff it in git, or debug with `cat`. Nothing is a black box.

| vs. Other Frameworks | Crewplane |
|---------------------|------------------|
| **Hidden state** | Externalized to markdown — fully auditable |
| **Tight SDK coupling** | CLI-first — zero provider lock-in |
| **Black box debugging** | Inspect `.crewplane/execution-stages/` at any step |
| **Adapter boilerplate** | Works with any CLI that reads/writes files |

> ⚠️ **Security Note:** `{{file:path}}` templates are restricted to the project root by default. Use `settings.integrations.artifacts.options.allowed_template_paths` for explicit external-file allowlisting.

## Features

- 🔄 **DAG Execution** – Run independent nodes in parallel and dependency nodes in sequence
- 🔍 **Cross-Review** – Agents review each other's outputs with structured verdict detection
- 📝 **Task Files** – Frontmatter+Markdown (`.task.md`) workflows by default
- 🔌 **Pluggable Providers** – Works with any CLI-based AI tool; no API keys or auth managed by Crewplane
- 📁 **Project-Local Config** – Each project gets its own `.crewplane/` directory
- 📂 **Transparent Artifacts** – Every intermediate step and final result saved to disk for full auditability
- 📊 **Spend Observability** – Run logs capture CLI capture status, provider token-report status, visible lower-bound estimates, and configured cost confidence summaries
- ⚡ **Smart Caching** – Workflow-signature idempotency skips identical successes and resumes failed or cancelled runs from validated node boundaries
- 🧪 **Experimental Workspace Isolation** – Opt-in Git-backed worktrees and writable snapshots can isolate provider source-tree edits in ordinary supported Git repositories

> For a deeper look at the architecture behind these features, see [Documentation index](https://github.com/crewplaneai/crewplane/blob/main/docs/index.md).

## Prerequisites

Before using Crewplane, you must have at least one AI CLI tool installed and authenticated.

Verify your providers are working:
```bash
which claude codex gemini copilot  # Check they're in PATH
claude --version                    # Verify Claude
copilot version                     # Verify Copilot CLI is installed
```

## Installation

The public package name is `crewplane`. The installed command is
`crewplane`.

Recommended isolated install:

```bash
uv tool install crewplane
crewplane --help
```

Crewplane can also be installed with the following supported methods:

```bash
# pipx
pipx install crewplane

# pip
python -m pip install crewplane

# install script for macOS and Linux
curl -fsSL https://raw.githubusercontent.com/crewplaneai/crewplane/main/install.sh | sh

# Homebrew
brew tap crewplaneai/crewplane && brew install crewplane

# npm alpha wrapper
npm install -g crewplane@alpha
```

For a local checkout:

```bash
git clone https://github.com/crewplaneai/crewplane.git
cd crewplane
python -m pip install .
```

Provider CLIs are installed and authenticated separately. Crewplane does not install provider CLIs, does not manage provider credentials, and does not sandbox provider CLI execution.

See the [installation guide](https://github.com/crewplaneai/crewplane/blob/main/docs/getting-started/installation.md) for update, uninstall, and npm `PATH` troubleshooting.

## First Run

```bash
crewplane init
crewplane validate
crewplane run --dry-run
crewplane run
```

`crewplane init` creates `.crewplane/config.yml`, a default workflow, and
additional example templates under `.crewplane/workflows/example-templates/`.

## Workflow Shape

```yaml
---
schema_version: "<current>"
name: "Quick Review"
nodes:
  - id: review.context
    mode: parallel
    providers: ["claude", "codex"]
---

## review.context
Review the current repository and report the highest-risk issues.
```

Full workflow authoring docs are in the
[workflow syntax reference](https://github.com/crewplaneai/crewplane/blob/main/docs/reference/workflow-syntax.md).

## Safety Boundary

Crewplane coordinates provider CLIs; it is not a security sandbox. Provider CLIs
run with the permissions, approval mode, network access, and filesystem access
configured for those tools.

Experimental workspace isolation can move selected provider source-tree work
into Git-backed worktrees or writable snapshots, but it is still source-tree
isolation only. It does not sandbox provider execution.

`{{file:path}}` template references are bounded to the project root by default.
External files must be explicitly allowlisted with
`settings.integrations.artifacts.options.allowed_template_paths`.

## Where Next

- [Documentation index](https://github.com/crewplaneai/crewplane/blob/main/docs/index.md)
- [Installation](https://github.com/crewplaneai/crewplane/blob/main/docs/getting-started/installation.md)
- [Quickstart](https://github.com/crewplaneai/crewplane/blob/main/docs/getting-started/quickstart.md)
- [Provider setup](https://github.com/crewplaneai/crewplane/blob/main/docs/getting-started/provider-setup.md)
- [Examples](https://github.com/crewplaneai/crewplane/blob/main/docs/examples/index.md)
- [Experimental workspace isolation](https://github.com/crewplaneai/crewplane/blob/main/docs/guides/workspace-isolation.md)
- [Command reference](https://github.com/crewplaneai/crewplane/blob/main/docs/reference/commands.md)
- [Configuration reference](https://github.com/crewplaneai/crewplane/blob/main/docs/reference/configuration.md)
- [Workflow syntax reference](https://github.com/crewplaneai/crewplane/blob/main/docs/reference/workflow-syntax.md)
- [Artifact reference](https://github.com/crewplaneai/crewplane/blob/main/docs/reference/artifacts.md)
- [Security and trust](https://github.com/crewplaneai/crewplane/blob/main/docs/safety/security-and-trust.md)
- [Architecture](https://github.com/crewplaneai/crewplane/blob/main/docs/architecture/index.md)
- [Contributing and local development](https://github.com/crewplaneai/crewplane/blob/main/DEVELOPMENT.md)
