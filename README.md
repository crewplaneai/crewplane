<div align="center">
  <h1>Crewplane</h1>
  <p><strong>Agents do the work. You own the workflow.</strong></p>
  <p>
    Crewplane is an open-source workflow runner for reviewable, resumable
    coding-agent workflows. Define the whole process in Markdown — the prompts,
    stages, agents, handoffs, and rules for what happens next.
    Crewplane runs Claude Code, Codex, Copilot CLI, Gemini, Kilo, or another CLI.
    Review becomes a gate, completed work survives failure, and every handoff
    stays on disk.
  </p>
  <p>
    <a href="https://github.com/crewplaneai/crewplane/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/crewplaneai/crewplane/actions/workflows/ci.yml/badge.svg?branch=master"></a>
    <a href="https://www.bestpractices.dev/projects/13966"><img src="https://www.bestpractices.dev/projects/13966/badge"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/pyproject.toml"><img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-3776AB.svg"></a>
    <a href="https://github.com/crewplaneai/crewplane/blob/master/docs/index.md"><img alt="Docs" src="https://img.shields.io/badge/docs-read-0f766e.svg"></a>
  </p>
</div>

<div align="center">
  <a href="#demo-walkthrough">
    <img
      src="https://github.com/user-attachments/assets/dca2dacb-49e4-4849-b92b-7a47b493ea52"
      alt="Crewplane dashboard showing a coding-agent workflow in progress"
      width="80%"
    >
  </a>
  <p>
    <em>Watch the workflow run. Keep the full record after the terminal closes.
    Click the dashboard for the walkthrough.</em>
  </p>
  <p>
    ⭐ If this is how you think coding-agent workflows should work,
    click <strong>Star</strong> in the top-right to keep Crewplane in your
    toolbox — and help more developers find it.
  </p>
  <p>
    <a href="#quick-start"><strong>Try it without API keys</strong></a>
    &nbsp;·&nbsp;
    <a href="https://github.com/crewplaneai/crewplane-lab/tree/master/leetcode-3348-gpt-8-agents"><strong>Inspect a real 8-agent run</strong></a>
    &nbsp;·&nbsp;
    <a href="docs/index.md">Read the documentation</a>
  </p>
</div>

## Your agents are automated. Your workflow usually isn't.

Coding-agent CLIs can plan, write, test, and review code. But the process around
them often still lives across prompts, terminal tabs, copy-paste, and memory.

You decide what runs next. You carry plans and findings between tools. You make
sure review actually happens. When a later step fails, you reconstruct what
already worked.

**Crewplane turns that manual coordination into a workflow your repository
owns.** Make review a gate, not a promise buried in a prompt. If a later stage
fails, Crewplane keeps the completed work it can validate instead of making you
start over.

| Without Crewplane                                         | With Crewplane                                                          |
| --------------------------------------------------------- | ----------------------------------------------------------------------- |
| Prompts, sessions, and terminal habits define the process | Versioned Markdown defines the process                                  |
| You relay plans, findings, and context between agents     | Each stage receives explicit, saved handoffs                            |
| One session or provider tends to own the whole chain      | Assign the right CLI to each stage                                      |
| Parallel work and review happen ad hoc                    | Fan work out, then bring it back through review and bounded fixes        |
| A late failure makes you replay work that already passed  | Resume from completed stages Crewplane can validate                     |
| Terminal scrollback becomes the history                   | Keep a readable record of every stage, handoff, finding, and result      |

> [!NOTE]
> **Bring the agent setup you already trust.** Crewplane invokes the coding-agent
> CLIs you already use; their models, tools, skills, MCP servers, repository
> instructions, authentication, and permissions remain under their native control.

## Eight agents. One comparison. Every receipt.

[Crewplane Lab](https://github.com/crewplaneai/crewplane-lab) publishes
reproducible experiments with the workflow definition, generated responses,
comparison output, telemetry, manifests, and provider logs.

Eight Codex model and reasoning configurations. One algorithm challenge. Watch
them work it in parallel—then follow every saved output into a dedicated
comparison stage.

<p align="center">
  <a href="https://github.com/crewplaneai/crewplane-lab/tree/master/leetcode-3348-gpt-8-agents">
    <img
      src="https://raw.githubusercontent.com/crewplaneai/crewplane-lab/master/docs/images/eight-agent-run.png"
      alt="Eight agent runs converging into one comparison stage, followed by preserved workflow, response, telemetry, and log artifacts"
      width="90%"
    >
  </a>
</p>

<p align="center">
  <a href="https://github.com/crewplaneai/crewplane-lab/tree/master/leetcode-3348-gpt-8-agents">
    <strong>Explore the complete recorded experiment →</strong>
  </a>
</p>

**Inspect the evidence:**
[workflow](https://github.com/crewplaneai/crewplane-lab/blob/master/leetcode-3348-gpt-8-agents/.crewplane/workflows/gpt-models-reasoning-comparison.task.md)
·
[eight responses](https://github.com/crewplaneai/crewplane-lab/blob/master/leetcode-3348-gpt-8-agents/.crewplane/execution-results/gpt-model-and-reasoning-comparison--e32358d21c7f-20260807-010927/reasoning.runs-result.md)
·
[comparison](https://github.com/crewplaneai/crewplane-lab/blob/master/leetcode-3348-gpt-8-agents/.crewplane/execution-results/gpt-model-and-reasoning-comparison--e32358d21c7f-20260807-010927/reasoning.compare-result.md)
·
[complete run record](https://github.com/crewplaneai/crewplane-lab/tree/master/leetcode-3348-gpt-8-agents/.crewplane/execution-stages/gpt-model-and-reasoning-comparison--e32358d21c7f-20260807-010927)

> [!TIP]
> **Explore the complete run without installing anything.** You do not need
> Crewplane or a provider account to inspect the saved artifacts. This is one
> recorded experiment rather than a general-purpose model benchmark.

## Quick start

Install Crewplane and run the generated workflow inside any project. The first
run lets you see the workflow and its run record ***before spending a token***:

```bash
uv tool install crewplane

cd path/to/your-project
crewplane init
crewplane validate
crewplane run
```

> [!IMPORTANT]
> The first run uses Crewplane's deterministic `mock` provider. **No provider
> CLI. No API key. No provider account. No token spend. No configuration changes.**

Inspect the resulting run record:

```text
.crewplane/
├── execution-results/<run-key>/   # findings and final results
└── execution-stages/<run-key>/    # inputs, outputs, logs, events, and manifests
```

**The output is mocked. The workflow machinery is not.** The run-record
structure is the same one used for provider-backed workflows, so you can see how
Crewplane validates stages, passes work forward, and records the run before
connecting a real agent.

Ready to put one of the CLIs already installed on your machine behind the
workflow?

```bash
crewplane onboarding
crewplane run
```

`crewplane onboarding` finds the supported provider CLIs already installed on
your machine, lets you choose one, and configures Crewplane to use it.
***Crewplane does not install provider software or manage provider credentials.***

<details>
<summary><strong>See exactly what your first run creates and keeps</strong></summary>

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

<br>

**Prefer to watch? [Jump to the full demo walkthrough →](#demo-walkthrough)**

## Build the workflow your work actually needs

Crewplane does not prescribe a lifecycle. It gives yours a home in the
repository, whether that means one focused stage or a longer workflow with
several agents and checkpoints.

For example:

- **Deliver a change with independent checks.**
  Brief → plan → implement → test → review → handoff.

- **Compare approaches before choosing one.**
  Ask several agents to explore the problem in parallel, then bring their
  proposals together for comparison and approval.

- **Audit a codebase from several angles.**
  Combine security, performance, architecture, and maintainability reviews
  into one consolidated result.

- **Carry a complex change through controlled checkpoints.**
  Inventory → design → migrate → validate → report.

- **Turn a recurring team process into a reusable workflow.**
  Keep the stages, agent assignments, handoffs, and review rules in the
  repository instead of rebuilding the process from terminal history.

These are examples, not built-in stages. Name the steps yourself, use one agent
throughout, or assign different CLIs wherever their strengths fit best.

## What a workflow looks like

A Crewplane workflow is Markdown you can read in a code review: YAML frontmatter
declares the execution graph, and ordinary Markdown defines the instructions for
each stage.

This is the real thing, not a hello-world toy: **one file, three stages, explicit
handoffs, and a review loop you can rerun.**

```markdown
---
schema_version: "1.0"
name: "Feature Delivery"
description: "Plan, implement, review, and prepare a final handoff."

nodes:
  - id: plan
    mode: parallel
    providers: [claude, codex]

  - id: implement
    mode: sequential
    needs: [plan]
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer

  - id: handoff
    mode: parallel
    needs: [implement]
    providers: [gemini]
---

## plan

Turn the request into a concrete plan with scope, risks, and validation steps.

## implement

Use the plan:

{{plan.output}}

<!-- crewplane:executor -->
Implement the change and run the relevant checks.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Review the candidate for correctness, regressions, and missing validation.
<!-- /crewplane:reviewer -->

## handoff

Prepare a concise handoff from the reviewed implementation:

{{implement.output}}
```

In this example:

* `needs` defines which stages wait for upstream work.
* `providers` assigns configured agents to each stage.
* `{{plan.output}}` creates an explicit handoff rather than relying on hidden
  session context.
* The sequential executor/reviewer node can approve the candidate or send
  blocking feedback into a bounded fix attempt.
* The final stage receives the reviewed result, not an informal summary carried
  between terminal sessions.

`plan`, `implement`, and `handoff` are examples — not built-in lifecycle stages.
Rename them, add more nodes, remove stages, fan work out, compose another
workflow, or route every stage to the same CLI.

See the [workflow syntax reference](docs/reference/workflow-syntax.md) for the
complete authoring contract.

> [!IMPORTANT]
> The agents keep their native tools and behavior. The workflow no longer has to
> live inside any one agent session.

## Every run leaves receipts

A final answer tells you what an agent said. A run record tells you how the work
actually happened. Crewplane keeps the answers to questions that disappear when
the process lives only inside terminal sessions:

<details>
<summary><strong>See what Crewplane records for every run</strong></summary>

* Which workflow and compiled execution plan ran?
* Which agent, role, and rendered context handled each stage?
* What output and findings did each stage produce?
* Which reviewers approved or blocked the candidate?
* Which nodes succeeded, failed, were skipped, resumed, or reused?
* Which provider-reported usage totals were available?

Check the results for each node:
```text
.crewplane/execution-results/<run-key>/
```

When needed, inspect the run-level records:

```text
.crewplane/execution-stages/<run-key>/
```

### See where the tokens went

At the end of a workflow, Crewplane displays the total token consumption in the
run summary. It also records each node's consumption in the runtime logs, so you
can see both the overall usage and where the tokens went. These figures appear
whenever the provider reports them.

> [!TIP]
> The files are ordinary local artifacts. Inspect them, diff them, archive them,
> attach them to a review, or delete them like other build output.

See:

* [Watch runs live and inspect results](docs/guides/watch-runs-live-and-inspect-results.md)
* [Inspect run records](docs/guides/inspecting-artifacts.md)
* [Artifact reference](docs/reference/artifacts.md)

</details>

## Where Crewplane fits

Crewplane is the **control plane around your coding-agent CLIs**.

It does not replace their models, tools, sessions, permissions, credentials, or
native execution behavior. It controls the process around them: what runs, in
what order, with which context, under which review rules, and what remains
afterward.

> [!TIP]
> **Choose the lightest tool that fits.**
>
> **Use a coding agent directly** for a quick question, one-off patch, or
> exploratory session. **Use a shell script** for a quick experiment,
> disposable automation, or a small workflow you do not expect to maintain
> long term.
>
> **Crewplane is built for reliable, reviewable, reusable agent workflows.**
> Define stages, dependencies, provider assignments, parallel work, review
> gates, and handoffs in Markdown. Crewplane validates and runs the graph,
> records the artifacts and manifests, and reuses completed stages it can
> validate—without turning a shell script into a workflow engine.

```text
┌──────────────────────────────────────────────┐
│ Workflow owned by your repository            │
│ Markdown · instructions · policies · inputs  │  ← Markdown defines the workflow.
└──────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────┐
│ Crewplane                                    │
│ validate · render · route · review           │
│ resume · observe · record                    │  ← Crewplane enforces the workflow.
└──────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────┐
│ Provider-native coding-agent CLIs            │
│ Claude Code · Codex · Copilot CLI · Gemini   │  ← Agents execute the stages.
└──────────────────────────────────────────────┘
                    ↕
┌──────────────────────────────────────────────┐
│ Repository · filesystem · CI                 │
│ source · tests · logs · manifests · results  │  ← Artifacts stay on disk.
└──────────────────────────────────────────────┘
```

The agents do the work. Crewplane runs the declared process. Your
repository remains the system of record.

## Installation and updates

The recommended installation is:

```bash
uv tool install crewplane
```

Update the active installation and confirm its version:

```bash
crewplane --update
crewplane --version
```

Other supported installation paths include pip, pipx, Homebrew, the install
script, an npm wrapper, and a local source checkout. See the
[installation guide](docs/getting-started/installation.md) for exact commands,
update behavior, troubleshooting, and removal.

> [!NOTE]
> Crewplane requires Python 3.13 or later and supports Linux, macOS, and WSL.
> ***Native Windows is not currently supported.***

> [!IMPORTANT]
> Crewplane does not install, authenticate, or sandbox provider CLIs. Each
> provider runs with its own configuration and the permissions available in
> your environment.

### Demo walkthrough

Watch the demo below for the full setup flow: install Crewplane, initialize a project, run the first mock workflow, inspect artifacts, and onboard a real provider.

<p align="center" style="margin-bottom: 0;">
  <video src="https://github.com/user-attachments/assets/b6573226-ba31-473e-aaae-ba3ddca2d3cd" autoplay loop muted playsinline width="1000"></video>
</p>

## Documentation and examples

The full documentation starts at [docs/index.md](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md).

**Just getting started?** → follow the
[First Project Path](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md#first-project-path)
to install Crewplane, run the mock workflow, inspect artifacts, and prepare a
real provider.

**Guided tour:** → use the
[Guided Tutorial Track](https://github.com/crewplaneai/crewplane/blob/master/docs/index.md#guided-tutorial-track)
to walk through workflow runs, run records, authoring, provider roles, review
loops, composition, validation, troubleshooting, and cleanup.

<details>
<summary><strong>Run the full examples without starting provider CLIs</strong></summary>

**Want to see Crewplane at full strength?** → start with one of the generated
workflows:

- `example-templates/code-review-example.task.md` for parallel agent review and reviewer loops.
- `example-templates/feature-implement-example.task.md` for brief → plan → build → review → handoff.
- `example-templates/composition/review-fix-composed-example.task.md` for reusable workflow composition.

With `settings.integrations.invoker.implementation: "mock"`, Crewplane validates
those agent profiles but still writes deterministic mock output and does not
start provider CLIs. Switch the invoker to `cli` only when you want real provider
runs.

1. Uncomment the agents in the generated config (i.e. lines 22-148), keep the `settings.integrations.invoker.implementation` as `mock` so the workflow runs with mock. See
   [how to turn mock on and off](https://github.com/crewplaneai/crewplane/blob/master/docs/getting-started/provider-setup.md#turn-mock-mode-onoff)
   for details.

2. Copy and run any one of these commands:

```
crewplane run --tasks .crewplane/workflows/example-templates/code-review-example.task.md
```
```
crewplane run --tasks .crewplane/workflows/example-templates/feature-implement-example.task.md
```
```
crewplane run --tasks .crewplane/workflows/example-templates/composition/review-fix-composed-example.task.md
```

Ready to hook up a real provider? Run `crewplane onboarding`, or follow the
[provider setup guide](docs/getting-started/provider-setup.md).

</details>

<details>
<summary><strong>Find the right guide for what you want to do next</strong></summary>

Here is a quick reference table:

| Goal                                    | Start here                                                    |
| --------------------------------------- | ------------------------------------------------------------- |
| Complete the first project              | [First Project Path](docs/index.md#first-project-path)        |
| Follow the guided tutorial              | [Guided Tutorial Track](docs/index.md#guided-tutorial-track)  |
| Learn workflow authoring                | [Workflow syntax](docs/reference/workflow-syntax.md)          |
| Choose sequential or parallel execution | [Node modes and provider roles](docs/guides/node-modes.md)    |
| Add executor/reviewer behavior          | [Review loops](docs/guides/review-loops.md)                   |
| Configure real provider CLIs            | [Provider setup](docs/getting-started/provider-setup.md)      |
| Inspect stored execution records        | [Inspecting artifacts](docs/guides/inspecting-artifacts.md)   |
| Inspect a published provider-backed run | [Crewplane Lab](https://github.com/crewplaneai/crewplane-lab) |
| Try generated workflows                 | [Examples guide](docs/examples/index.md)                      |
| Browse all documentation                | [Documentation home](docs/index.md)                           |
| Review changes between releases         | [Changelog](CHANGELOG.md)                                     |

</details>

## Contributing

Contributions, workflow ideas, and real-world failure cases are welcome.

* Read [Contributing](CONTRIBUTING.md).
* Set up a local checkout with the [development guide](DEVELOPMENT.md).
* Ask questions or propose workflow patterns in
  [GitHub Discussions](https://github.com/crewplaneai/crewplane/discussions).

Have a coding-agent workflow that you do not want to leave to a free-running
loop? Describe it in Discussions and we can all work together on it.

---

<div align="center">
  <p><strong>Agents do the work. You own the workflow.</strong></p>
  <p>
    ⭐ Have a coding-agent process worth making repeatable?
    Click <strong>Star</strong> in the top-right to keep Crewplane in your toolbox.
  </p>
</div>
