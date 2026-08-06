<div align="center">
  <h1>Crewplane</h1>
  <p><strong>Turn AI agent calls into structured, repeatable workflows.</strong></p>
  <p>
    Define multi-step workflows in Markdown. Run each stage through Claude Code,
    Codex, Gemini, Copilot, or any CLI. Resume from where you left off. Keep
    every input, output, and decision on disk.
  </p>
  <p>
    <a href="https://github.com/crewplaneai/crewplane/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/crewplaneai/crewplane/actions/workflows/ci.yml/badge.svg?branch=master"></a>
    <a href="https://www.bestpractices.dev/projects/13966"><img src="https://www.bestpractices.dev/projects/13966/badge"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/pyproject.toml"><img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-3776AB.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/docs/index.md"><img alt="Docs" src="https://img.shields.io/badge/docs-read-0f766e.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/crewplaneai/crewplane?style=social"></a>
  </p>
</div>

<div align="center">
  <img src="https://github.com/user-attachments/assets/dca2dacb-49e4-4849-b92b-7a47b493ea52" alt="Crewplane Dashboard View" width="80%">
</div>

---

## Why Crewplane?

You already have the pieces: repo instructions, skills, MCP servers,
provider settings, internal conventions. **The agents know how to work.
What's missing is the control plane: _when_ to work, _in what order_,
and _what to do when something breaks._**

Define the DAG in Markdown, assign each stage to Claude Code, Codex, Gemini,
Copilot, or any CLI. Crewplane runs exactly the plan you wrote, saves every
stage to disk, and picks up where a failed run left off. No SDK, no framework
lock-in, no autonomous loop deciding what happens next.

### What changes when you add Crewplane

| Agent work today | With Crewplane |
| --- | --- |
| One long session | Markdown DAG with sequential and parallel stages |
| One provider at a time | Claude Code, Codex, Gemini, Copilot, or any CLI |
| Copy-pasted prompts | Rendered inputs saved under `.crewplane/` |
| Terminal scrollback | Outputs, logs, manifests, and final results on disk |
| Start over after failure | Resume from validated stage boundaries |
| Hard-to-follow progress | Optional tmux dashboard |
| Edits in the project root | Optional Git-backed worktrees and snapshots |

> ***The result is a run record you can inspect, diff, archive, attach to a review, or delete like any other build output.***

> **CLI-first by design.**
> Crewplane invokes provider CLIs directly instead of wrapping them in a vendor SDK or agent framework. If a tool has a command line, Crewplane can run it.

<details>
<summary><strong>When should you just use one agent CLI?</strong></summary>

For a quick question, a one-off patch, or exploratory work that fits in a single session — use the provider directly. Crewplane is for the moment agent work becomes a **process**: multiple stages, provider handoffs, review loops, or runs that need to survive failure and remain auditable.

</details>

### Where it fits in your stack

```
┌──────────────────────────────────────────────┐
│ Developer / Team Intent                      │
│ Markdown workflow · policies · approvals     │  ← Markdown defines the workflow.
└──────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────┐
│ Crewplane - Control Plane                    │
│ preflight · DAG · routing · resume · receipts│  ← Crewplane enforces the graph.
└──────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────┐
│ Agent Execution Plane                        │
│ Claude Code · Codex · Copilot CLI · Gemini   │  ← Agents execute the stages.
└──────────────────────────────────────────────┘
                    ↕ read/write
┌──────────────────────────────────────────────┐
│ Repo / Filesystem / CI                       │
│ source · tests · logs · manifests · results  │  ← Artifacts stay on disk.
└──────────────────────────────────────────────┘
```

## Install

```bash
uv tool install crewplane
```

Update Crewplane from any directory, then print the installed version:

```bash
crewplane --update
crewplane --version
```

Crewplane verifies which supported package manager installed the copy you are
running, then runs that manager's standard upgrade command. Automatic updates
support `uv tool` (including the install script), `pipx`, and Homebrew. For a
global `npm` installation, Crewplane prints manual update instructions instead.
See the
[installation guide](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/installation.md#update)
for supported installation methods, failure behavior, and manual commands.

<details>
<summary><strong>Other install methods</strong></summary>

```bash
# pip
python -m pip install crewplane

# npm
npm install -g crewplane
```

Other methods (pipx, install script, local checkout) are documented in the
[installation guide](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/installation.md).

</details>


> ⚠️ Crewplane does **not** install or manage provider CLIs or credentials. Install and authenticate Claude Code, Copilot CLI, etc. separately.

## Quick Start

From a project directory:

```bash
crewplane init       # scaffold a project with a mock workflow
crewplane validate   # check the workflow DAG
crewplane run        # execute — no API keys needed
```

Inspect the run record:

```
.crewplane/
├── execution-results/<run>/   # final outputs: findings, results
└── execution-stages/<run>/    # per-stage inputs, outputs, logs, manifests
```

That's it. The first run uses a deterministic `mock` provider — no provider CLIs,
API keys, or config edits required.

<details>
<summary><strong>More on the first run</strong></summary>

`crewplane init` creates `.crewplane/config.yml`, a default workflow, and
additional example templates under `.crewplane/workflows/example-templates/`.

When output is attached to a terminal and `tmux` is available, Crewplane opens
the compact live dashboard for DAG progress, node status, and live log tails.

> **Note:** install `tmux` via `brew install tmux` on *macOS* or
> `sudo apt install tmux` on *Ubuntu/Debian*.

> Pass `--no-live` when you want to omit the live dashboard.

After the first run, the full artifact layout looks like:

```
.crewplane/
├── execution-results/                  # final outputs you care about
│   └── <run-key>/
│       ├── review.project-findings.md  # findings from the review node
│       └── review.project-result.md    # final result from the review node
├── execution-stages/                   # per-stage raw artifacts
│   └── <run-key>/
│       ├── preflight/                  # plan, dependency graph, render plans
│       ├── logs/                       # events.ndjson, summary
│       └── review.project/             # per-node rendered input, output, logs
├── workflows/                          # your workflow definitions, preloaded with example workflows
│   └── single-agent-review.task.md
└── config.yml                          # provider wiring and settings
```

These files are the same shape you will see with real providers: each step has
rendered inputs, outputs, logs, manifests, and final results you can inspect or
diff with normal tools.

Because the first run already wrote a successful result, a later identical run
may print `Identical context detected` (Crewplane reuses the saved result for
identical inputs). Use `crewplane run --force` to start fresh.

</details>

## What a Workflow Looks Like

Workflows are Markdown files that live in your repo. Review them in a PR,
version them with your code, share them across teams.

```yaml
---
schema_version: "1.0"
name: Single Agent Review
description: One deterministic mock review node for the first Crewplane run.
nodes:
  - id: review.project
    mode: parallel
    findings: true
    providers: ["mock"]
---

## review.project
Review the current repository and report the highest-risk issues.
```

Full workflow authoring docs are in the
[workflow syntax reference](https://github.com/crewplaneai/crewplane/blob/master/docs/reference/workflow-syntax.md).

## Prepare a Real Provider

After the mock run succeeds, use `onboarding` to wire up a real provider:

```bash
crewplane onboarding
```

Onboarding detects provider CLIs on `PATH`, lets you choose one, and updates
the generated config. It does not start provider CLIs or authenticate them —
install and authenticate Claude Code, Copilot CLI, etc. separately.

After onboarding, run the workflow with the selected provider:

```bash
crewplane run
```

If you need multiple providers or manual setup, see the
[provider setup guide](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/provider-setup.md).

> **Note:** Treat run artifacts like build outputs: useful for debugging and
> review, but decide separately what, if anything, belongs in version control.

### Demo walkthrough

Watch the demo below for the full setup flow: install Crewplane, initialize a project, run the first mock workflow, inspect artifacts, and onboard a real provider.

<p align="center" style="margin-bottom: 0;">
  <video src="https://github.com/user-attachments/assets/b6573226-ba31-473e-aaae-ba3ddca2d3cd" autoplay loop muted playsinline width="1000"></video>
</p>

---

At this point you have seen the core path: install, run the generated mock
workflow, inspect artifacts, and prepare a real provider when ready.

## Learn More

The full documentation starts at [docs/index.md](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md).

**Just getting started?** → follow the
[First Project Path](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md#first-project-path)
to install Crewplane, run the mock workflow, inspect artifacts, and prepare a
real provider.

**Guided tour:** → use the
[Guided Tutorial Track](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md#guided-tutorial-track)
to walk through workflow runs, run records, authoring, provider roles, review
loops, composition, validation, troubleshooting, and cleanup.

**Want to see what Crewplane can orchestrate?** → try these generated workflows after enabling their provider names in `.crewplane/config.yml`:

- `example-templates/code-review-example.task.md` for parallel agent review and reviewer loops.
- `example-templates/feature-implement-example.task.md` for brief → plan → build → review → handoff.
- `example-templates/composition/review-fix-composed-example.task.md` for reusable workflow composition.

With `settings.integrations.invoker.implementation: "mock"`, Crewplane validates those agent profiles but still writes deterministic mock output and does not start provider CLIs. Switch the invoker to `cli` only when you want real provider runs.

<details>
<summary><strong>How to run them with mock</strong></summary>

1. Uncomment the agents in the generated config (i.e. lines 22-138), keep the `settings.integrations.invoker.implementation` as `mock` so the workflow runs with mock. See [how to turn mock on and off](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/provider-setup.md#turn-mock-mode-onoff) for details.

2. Use the following commands to try it out.

Copy pastable commands for the workflows:
```
crewplane run --tasks .crewplane/workflows/example-templates/code-review-example.task.md
```
```
crewplane run --tasks .crewplane/workflows/example-templates/feature-implement-example.task.md
```
```
crewplane run --tasks .crewplane/workflows/example-templates/composition/review-fix-composed-example.task.md
```
</details>

For more workflows, see the
[Examples guide](https://github.com/crewplaneai/crewplane/blob/master/docs/examples/index.md);
for exact flags, config keys, workflow syntax, and artifact formats, use the
[Reference](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md#reference).

## Contributing

Interested in contributing? Start with [Contributing and local development](https://github.com/crewplaneai/crewplane/blob/master/DEVELOPMENT.md).

Questions and workflow ideas are welcome in
[GitHub Discussions](https://github.com/crewplaneai/crewplane/discussions).
Have a coding-agent workflow you don't want to leave to a free-running loop?
Describe it there — good examples can become Crewplane templates.

---

<div align="center">
  <p>
    If you believe coding-agent workflows should be files you can review, reuse,
    and run locally — <a href="https://github.com/crewplaneai/crewplane"><b>star this repo</b></a> ⭐ so more developers can find it.
  </p>
</div>
