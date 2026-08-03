# Crewplane for Homebrew

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

## Install With Homebrew

On macOS:

```bash
brew tap crewplaneai/crewplane
brew install crewplane
crewplane --help
```

## Run Your First Workflow

Move into a project directory and run:

```bash
crewplane init       # create local config and example workflows
crewplane validate   # compile and validate the workflow DAG
crewplane run        # run with deterministic mock output
```

The generated workflow uses Crewplane's mock invoker, so the first run needs no
provider CLI, account, API key, token spend, or config edit. It still writes the
same inspectable run-record structure used for real provider runs:

```text
.crewplane/
├── execution-results/<run>/   # consolidated outputs and findings
└── execution-stages/<run>/    # inputs, outputs, logs, and manifests
```

After the mock run succeeds, install and authenticate a provider CLI separately
and prepare the project for a real run:

```bash
crewplane onboarding
crewplane run
```

Crewplane does not install provider CLIs, manage credentials, grant
permissions, or sandbox provider execution. It coordinates the workflow around
the provider tools and policies you already trust.

Use a provider CLI directly for a quick question or one-off patch. Use
Crewplane when the work has multiple stages, provider handoffs, review loops,
failure recovery, or a run record that other people need to inspect.

## Update

Crewplane can ask Homebrew to update the installed copy and then report the
version:

```bash
crewplane --update
crewplane --version
```

You can also use Homebrew directly:

```bash
brew upgrade crewplane
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

## Tap Maintainer Notes

This directory contains the formula source intended for the external tap at
`https://github.com/crewplaneai/homebrew-crewplane`. This repository does not
create or push that external tap.

Before publishing the tap, use the exact canonical PyPI artifact and dependency
resources that will be served publicly:

1. Run `make release-prepare` for a coordinated new version.
2. Confirm the prepared formula points at the canonical PyPI sdist and SHA.
3. Run `make release-check`.
4. Copy `packaging/homebrew/Formula/crewplane.rb` into the tap repository.
5. Run `brew audit --strict crewplane` and `brew test crewplane` from the tap.
6. Push the tap update after PyPI and npm are live.

For local validation before publication:

```bash
make brew-smoke
```

Release and smoke targets read the version from `pyproject.toml`.
`make release-prepare` verifies that the exact version is not already on PyPI
or npm before it rewrites local release scratch state. `make release-pypi` and
`make release-npm` run registry-specific remote checks so a partial release can
be completed without being blocked by a version that already exists in the
other registry.

The local smoke target creates a temporary formula copy that points at the
local sdist. It skips clearly when Homebrew is unavailable or a `crewplane`
formula is already installed. The formula declares `maturin` for the
`pydantic-core` runtime sdist, installs declared runtime resources first, and
then builds the `crewplane` sdist with build isolation disabled so the
Hatchling backend comes only from the pinned formula resources.
