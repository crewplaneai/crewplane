# Crewplane

**Turn AI agent calls into structured, repeatable workflows.**

Crewplane is a provider-neutral control plane for human-designed coding-agent workflows. Define a multi-stage workflow in Markdown, assign each stage to Claude Code, Codex, Gemini, Copilot, Kilo, or another command-line tool, and let Crewplane handle ordering,
parallelism, handoffs, and resume.

Every rendered input, provider output, log, manifest, and final result stays on
disk under `.crewplane/`. The workflow says what should run; the run record
shows what did run.

<p align="center">
  <img src="https://github.com/user-attachments/assets/dca2dacb-49e4-4849-b92b-7a47b493ea52" alt="Crewplane live workflow dashboard" width="80%">
</p>

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

## Install With npm

The npm wrapper supports macOS, Linux, and WSL, and requires Node.js 18 or
newer. Native Windows is not supported.

```bash
npm install -g crewplane
crewplane --help
```

You can also try the command without a global install:

```bash
npx crewplane --help
```

The npm postinstall step bootstraps `uv` when needed, creates a private Python
3.13 environment inside the package, and installs the matching Crewplane
release. This keeps Crewplane isolated from your project dependencies.

## Run Your First Workflow

From a project directory:

```bash
crewplane init       # create local config and example workflows
crewplane validate   # compile and validate the workflow DAG
crewplane run        # run with deterministic mock output
```

The generated first run is intentionally provider-free. It does not require a
provider CLI, account, API key, token spend, or config edit. It writes the same
kind of run record that a real provider run will create:

```text
.crewplane/
├── execution-results/<run>/   # consolidated outputs and findings
└── execution-stages/<run>/    # inputs, outputs, logs, and manifests
```

## Workflows Are Markdown

Workflow files live with your project, so you can review them in pull requests,
reuse them across runs, and change the provider without rewriting the process.

```yaml
---
schema_version: "1.0"
name: Project Review
nodes:
  - id: review.project
    mode: parallel
    findings: true
    providers: ["mock"]
---

## review.project
Review the current repository and report the highest-risk issues.
```

Crewplane supports sequential and parallel stages, explicit dependencies,
executor/reviewer loops, reusable workflow composition, file inputs, and
artifact references between nodes.

## Connect a Real Provider

After the mock workflow succeeds, install and authenticate your chosen provider
CLI separately, then let Crewplane prepare the generated project:

```bash
crewplane onboarding
crewplane run
```

Onboarding detects supported provider commands on `PATH`, lets you select one,
and updates the generated config. Crewplane does not install provider CLIs,
manage credentials, grant permissions, or sandbox provider execution.

For a quick question, exploratory work, or a patch that fits in one session,
keep using the provider CLI directly. Use Crewplane when the ordering,
handoffs, failure recovery, or audit trail matter.

## npm Wrapper Notes

This npm package exposes the Python `crewplane` application through a Node.js
shim. Both `crewplane` and `npx crewplane` delegate to the same console command
inside the package's private environment.

Global npm installs create shims under `$(npm config get prefix)/bin`. If npm
reports a successful install but your shell cannot find `crewplane`, add that
directory to `PATH` and confirm Node.js remains available:

```bash
npm_prefix="$(npm config get prefix)"
export PATH="$npm_prefix/bin:$PATH"
command -v node
command -v crewplane
```

If npm lifecycle scripts are disabled, the private environment is not created.
Enable lifecycle scripts and recover with:

```bash
npm rebuild --global crewplane
```

Update a global npm installation with:

```bash
npm update --global crewplane
```

### Maintainer smoke check

Set `CREWPLANE_INSTALL_PYTHON` only when a local smoke check must use a specific
Python executable. To install a locally packed wrapper against a local
wheelhouse:

```bash
CREWPLANE_INSTALL_FIND_LINKS=/path/to/wheelhouse \
CREWPLANE_INSTALL_NO_INDEX=1 \
npm install -g ./crewplane-0.1.7.tgz
```

## Learn More

- [Why Crewplane?](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/why-crewplane.md)
- [Quickstart](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/quickstart.md)
- [Provider setup](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/provider-setup.md)
- [Workflow syntax](https://github.com/crewplaneai/crewplane/blob/master/docs/reference/workflow-syntax.md)
- [GitHub repository](https://github.com/crewplaneai/crewplane)

Crewplane is open source under the Apache-2.0 license. Questions and workflow
ideas are welcome in
[GitHub Discussions](https://github.com/crewplaneai/crewplane/discussions).
