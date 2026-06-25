<div align="center">
  <h1>Crewplane</h1>
  <p>A Markdown-native control plane for AI coding CLIs.</p>
  <p>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/CHANGELOG.md"><img alt="Status: alpha" src="https://img.shields.io/badge/status-alpha-f59e0b.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/pyproject.toml"><img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-3776AB.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/docs/index.md"><img alt="Docs" src="https://img.shields.io/badge/docs-read-0f766e.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/SECURITY.md"><img alt="Security policy" src="https://img.shields.io/badge/security-policy-b91c1c.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/CONTRIBUTING.md"><img alt="Contributing" src="https://img.shields.io/badge/contributing-guide-6b7280.svg"></a>
  </p>
  <img src="https://raw.githubusercontent.com/crewplaneai/crewplane/master/.github/crewplane-splash.png" alt="Crewplane splash" width="80%">
</div>

---

## Why Crewplane?

You've got powerful AI tools at your fingertips — Claude Code, Codex CLI, Gemini CLI, and others. Each has its strengths, but tying their work into a single repeatable pipeline usually means shell scripts, brittle glue, and state scattered across temp files.

**Crewplane gives your project a control plane for your agent crews.** Write the workflow once in Markdown, assign each step to whichever AI agent fits best, and Crewplane handles the rest — parallel, fan-out, sequential handoffs, retries, and a complete paper trail.

No SDKs. No plugins. No vendor lock-in. If your AI tool has a CLI, it works.

**Every step is a Markdown file.** Inputs, outputs, reviews — all saved to `.crewplane/execution-stages/` as readable Markdown. Inspect any step in your editor, diff it in git, or debug with `cat`. Nothing is a black box.

| vs. Other Frameworks | Crewplane |
|---------------------|------------------|
| **Autonomous loops** | Explicit DAG control — define exactly how agents work together, in sequence or in parallel |
| **Hidden state** | Externalized to markdown — fully auditable |
| **Tight SDK coupling** | CLI-first — zero provider lock-in |
| **Black box debugging** | Inspect `.crewplane/execution-stages/` at any step, final results saved in `.crewplane/execution-results/` |
| **Adapter boilerplate** | Works with any CLI that reads/writes files |

> *"Crewplane doesn't try to understand your AI tools. It just gives them a shared workspace and gets out of the way."*

> ⚠️ **Security Note:** `{{file:path}}` templates are restricted to the project root by default. Use `settings.integrations.artifacts.options.allowed_template_paths` for explicit external-file allowlisting.

## Installation

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
curl -fsSL https://raw.githubusercontent.com/crewplaneai/crewplane/master/install.sh | sh

# Homebrew
brew tap crewplaneai/crewplane && brew install crewplane

# npm wrapper
npm install -g crewplane
```

For a local checkout:

```bash
git clone https://github.com/crewplaneai/crewplane.git
cd crewplane
python -m pip install .
```

> ⚠️ **Note:** Provider CLIs are installed and authenticated separately. Crewplane does not install provider CLIs, does not manage provider credentials, and does not sandbox provider CLI execution.

See the [installation guide](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/installation.md) for update, uninstall, and npm `PATH` troubleshooting.

## First Run

```bash
crewplane init
crewplane validate
crewplane run --no-live
```

`crewplane init` creates `.crewplane/config.yml`, a default workflow, and
additional example templates under `.crewplane/workflows/example-templates/`.
The default run uses deterministic `mock` output (no cost) and does not require provider
CLIs, API keys, provider accounts, or config edits. It is scaffolding for
validating the workflow and artifact path, not model output.

Inspect the first run artifacts in `.crewplane/execution-stages/` and execution results in 
`.crewplane/execution-results/`, then follow
[provider setup](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/provider-setup.md)
when you are ready to run real provider CLIs.

## Live Dashboard

For interactive runs, omit `--no-live` to open Crewplane's compact tmux
dashboard (install tmux first if your system doesn't already have it).
Re-running the same workflow? Add `--force` to skip the duplicate check:

```bash
crewplane run --force
```

The dashboard shows the workflow DAG, node status, selected provider output,
and live log tails while the same durable artifacts are written under
`.crewplane/`. It starts only when tmux is available, output is attached to a
terminal, and provider log capture is enabled; otherwise Crewplane warns and
continues with normal execution. See the
[observability guide](https://github.com/crewplaneai/crewplane/blob/master/docs/guides/observability.md)
for dashboard options and log inspection.

## Workflow Shape

```yaml
---
schema_version: "<current>"
name: "Quick Review"
nodes:
  - id: review.project
    mode: parallel
    providers: ["mock"]
---

## review.project
Review the current repository and report the highest-risk issues.
```

Full workflow authoring docs are in the
[workflow syntax reference](https://github.com/crewplaneai/crewplane/blob/master/docs/reference/workflow-syntax.md).

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

The default mock run gives you the same workflow machinery used for real
provider runs, so you can inspect the shape of the system before connecting
external CLIs:

- 🔄 **DAG Execution** – Run independent nodes in parallel and dependency nodes in sequence
- 🔍 **Cross-Review** – Agents review each other's outputs with structured verdict detection
- 📝 **Task Files** – Frontmatter+Markdown (`.task.md`) workflows by default
- 🔌 **Pluggable Providers** – Works with any CLI-based AI tool; no API keys or auth managed by Crewplane
- 📁 **Project-Local Config** – Each project gets its own `.crewplane/` directory
- 📂 **Transparent Artifacts** – Every intermediate step and final result saved to disk for full auditability
- 🖥️ **Live Dashboard** – Optional tmux UI shows DAG progress, node status, and selected provider logs
- 📊 **Spend Observability** – Run logs capture CLI capture status, provider token-report status, visible lower-bound estimates, and configured cost confidence summaries
- ⚡ **Smart Caching** – Workflow-signature idempotency skips identical successes and resumes failed or cancelled runs from validated node boundaries
- 🧪 **Experimental Workspace Isolation** – Opt-in Git-backed worktrees and writable snapshots can isolate provider source-tree edits in ordinary supported Git repositories

When you are ready to configure a project, start with the quickstart and then
move into provider setup, examples, and reference material as needed:

- [Documentation index](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md)
- [Installation](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/installation.md)
- [Quickstart](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/quickstart.md)
- [Setup checklist](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/setup-checklist.md)
- [Provider setup](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/provider-setup.md)
- [Examples](https://github.com/crewplaneai/crewplane/blob/master/docs/examples/index.md)
- [Experimental workspace isolation](https://github.com/crewplaneai/crewplane/blob/master/docs/guides/workspace-isolation.md)
- [Command reference](https://github.com/crewplaneai/crewplane/blob/master/docs/reference/commands.md)
- [Configuration reference](https://github.com/crewplaneai/crewplane/blob/master/docs/reference/configuration.md)
- [Workflow syntax reference](https://github.com/crewplaneai/crewplane/blob/master/docs/reference/workflow-syntax.md)
- [Artifact reference](https://github.com/crewplaneai/crewplane/blob/master/docs/reference/artifacts.md)
- [Security and trust](https://github.com/crewplaneai/crewplane/blob/master/docs/safety/security-and-trust.md)

### Contributing

If you're interested in contributing to Crewplane, please read our [Contributing and local development](https://github.com/crewplaneai/crewplane/blob/master/DEVELOPMENT.md) before submitting a pull request.
