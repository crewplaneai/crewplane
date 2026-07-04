<div align="center">
  <h1>Crewplane</h1>
  <p><strong>A control plane for resumable coding-agent workflows.</strong></p>
  <p>
    Define workflows in Markdown, run each stage through Claude Code, Codex,
    Gemini, Copilot, or any CLI, and keep the run record on disk.
  </p>
  <p>
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

***The result is a run record you can inspect, diff, archive, attach to a review, or delete like any other build output.***

> **CLI-first by design.**
> Crewplane invokes provider CLIs directly instead of wrapping them in a vendor SDK or agent framework. If a tool has a command line, Crewplane can run it.

### Where it fits in your stack

```
┌─────────────────────────────────────────────────┐
│  Your repo: instructions, skills, MCP, config   │  ← agents read this
├─────────────────────────────────────────────────┤
│  Provider CLIs: Claude Code, Codex, Gemini, …   │  ← agents do the work
├─────────────────────────────────────────────────┤
│  Crewplane                                      │  ← sequences, resumes,
│    workflow DAG · stage artifacts · run record  │    records, coordinates
└─────────────────────────────────────────────────┘
```

<details>
<summary><strong>When should you just use one agent CLI?</strong></summary>

For a quick question, a one-off patch, or exploratory work that fits in a single session — use the provider directly. Crewplane is for the moment agent work becomes a **process**: multiple stages, provider handoffs, review loops, or runs that need to survive failure and remain auditable.

</details>

## Installation

Run the following on Mac or Linux to install Crewplane:

```bash
uv tool install crewplane
```

Crewplane can also be installed via the following package managers:

```bash
# pip
python -m pip install crewplane

# npm
npm install -g crewplane
```

> ⚠️ **Note:** Crewplane does **not** install or manage provider CLIs or credentials. Install and authenticate Claude Code, Copilot CLI, etc. separately.

Other install methods (pipx, install script, local checkout) are documented in the [installation guide](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/installation.md).

## First Run (No real agent invocations)

From a project directory:

```bash
crewplane init
crewplane validate
crewplane run
```

`crewplane init` creates `.crewplane/config.yml`, a default workflow, and
additional example templates under `.crewplane/workflows/example-templates/`.

`crewplane run` then runs the workflow with a deterministic `mock` provider.
The first run does not require provider CLIs, API keys, or config edits.

When output is attached to a terminal and `tmux` is available, Crewplane opens
the compact live dashboard for DAG progress, node status, provider output, and
live log tails. If `tmux` is missing, Crewplane warns and continues without the
dashboard.

> **Note:** install `tmux` via `brew install tmux` on *macOS* or
> `sudo apt install tmux` on *Ubuntu/Debian*.

> Pass `--no-live` when you want to omit the live dashboard.

Inspect the artifacts:

- Stage run files: `.crewplane/execution-stages/`
- Final results: `.crewplane/execution-results/`

After `crewplane run`, the key files under `.crewplane/` look like:

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

Mock output is scaffolding for validating Crewplane behavior, not model output.

> **Note:** Treat run artifacts like build outputs: useful for debugging and review, but decide separately what, if anything, belongs in version control.

Because the first run already wrote a successful result, a later identical run may print `Identical context detected`.
That means Crewplane found a previous successful run with the same inputs and reused the saved result instead of running the workflow again.
To start a fresh run, use:

```bash
crewplane run --force
```

## Workflow Shape

Workflows are Markdown+frontmatter; for example:

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

After the provider-free first run succeeds, use onboarding to prepare one real provider handoff:

```bash
crewplane onboarding
```

Onboarding detects provider CLI names on `PATH`, lets you choose one provider,
updates unchanged generated defaults, and validates the resulting Crewplane
wiring. It does not start real provider CLIs, authenticate providers, or check
provider account/model readiness.

After onboarding, run the workflow when the selected provider CLI is installed, authenticated, and ready to execute:

```bash
crewplane run
```

If you edited generated files, want multiple providers, or need manual setup, use
[provider setup](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/provider-setup.md).

At this point you have seen the core path: install, run the generated mock
workflow, inspect artifacts, and prepare a real provider when ready.

## Where Next

The full documentation starts at [Documentation](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md).

**Just getting started?** → follow the
[First Project Path](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md#first-project-path)
in the documentation index.

**Guided end-to-end tour** → check out
[Guided Tutorial Track](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md#guided-tutorial-track).
- It walks through running workflows, inspecting run records, authoring workflows,
provider roles, review loops, composition, validation, troubleshooting, and cleanup.

For exact flags, config keys, workflow syntax, and artifact formats, use the
[Reference](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md#reference)
section.

### Contributing

Interested in contributing? Start with [Contributing and local development](https://github.com/crewplaneai/crewplane/blob/master/DEVELOPMENT.md).
