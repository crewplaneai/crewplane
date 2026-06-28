# Why Crewplane?

Agent CLIs have become useful enough for real project work. The harder problem
is what happens when that work stops being one prompt and starts becoming a
process.

Planning, implementation, review, revision, synthesis, and follow-up often span
multiple agent calls. They may need different providers, different prompts,
review loops, and a record someone can inspect after the terminal scrollback is
gone.

Crewplane exists for that moment. It keeps your agent CLIs as the workers, then
adds the missing control plane around them: an explicit workflow, validated
stage boundaries, and a local record of what happened.

![Crewplane explains provider portability, traceable runs, duration control, and
fit with existing team processes.](../images/why_creplane.png)

## The Gap Between Prompt And Process

A single agent invocation can hold a plan in conversation context. A team
workflow needs something outside that conversation.

Without a workflow layer, the important control points become informal:

- Which step is allowed to start?
- Which earlier output is the next step using?
- Which provider made which decision?
- What finished before the run failed?
- What evidence remains for review, support, or compliance?

Crewplane moves those questions out of terminal memory and into project-local
files. The workflow says what should run. The run record shows what did run.

## The Design Principle

Crewplane is deliberately not another place to define agents in Python, hide
provider behavior behind SDK calls, or move team process into a hosted service.

The principle is simple: keep the tools teams already trust, and make the
workflow around those tools explicit.

| Concern | Crewplane's stance | Why it matters |
| --- | --- | --- |
| Agent capability | Use provider CLIs directly. | Your existing auth, approvals, tools, and models stay in charge. |
| Workflow control | Define stages and dependencies in Markdown. | The process can be reviewed before it runs. |
| State | Leave a durable filesystem trail. | You can inspect a run with normal development tools. |
| Failure | Resume from validated completed stages. | A long run does not have to become all-or-nothing work. |
| Team fit | Fit around repo instructions, skills, MCP, review, and policy. | Crewplane coordinates the process instead of replacing it. |

![Crewplane differs from typical SDK-first agent frameworks by staying
CLI-first, Markdown-defined, artifact-backed, and explicit about control
points.](../images/different_design.png)

For the architectural contract behind this stance, see
[Orchestration Model](orchestration-model.md).

## Why The Control Plane Should Be Separate

An agent can be excellent at deciding what to do next inside one task. That does
not make the agent conversation the right place to own the whole workflow.

Workflow control needs a more durable surface. It should be visible before a
run starts, clear when provider choices change, inspectable when something
fails, and usable by downstream stages without asking a previous chat transcript
to become the system of record.

That separation is what makes Crewplane useful. Agents remain responsible for
model work. Crewplane remains responsible for sequencing, handoffs, execution
state, and the artifact trail that makes the run understandable later.

## What A Run Becomes

With Crewplane, a run is no longer just a terminal session. It becomes a named
execution attempt with enough preserved context to understand what was asked,
what happened, and what can be reused.

That does not make every run permanent or public. It gives your project a normal
filesystem trail to inspect, clean up, archive, compare, or attach to a support
handoff according to your policy.

![Crewplane defines workflows in Markdown, runs provider CLIs per task, and
keeps an auditable filesystem record.](../images/control_plane.png)

For exact artifact paths and inspection starting points, see
[Inspecting Run Records](../guides/inspecting-artifacts.md).

## Trust Boundary

Crewplane coordinates selected workflows locally; it is not the layer that
enforces provider permissions or execution containment. The full safety and
privacy boundary lives in [Security And Trust](../safety/security-and-trust.md).

## When It Is Worth Using

Use Crewplane when agent work has enough shape that the process matters:

- multiple stages need clear ordering or parallelism
- one provider's output should feed another provider or reviewer
- a failed run should resume from validated completed stages
- you want a reusable workflow instead of a recreated prompt chain
- the team needs evidence of what was requested, produced, reviewed, and reused

For a quick question, a one-off patch, or exploratory work that fits comfortably
inside one provider session, use the provider CLI directly.

## Next

Run the provider-free [quickstart](../getting-started/quickstart.md), then read
[Workflows](workflows.md) when you are ready to author your own process.
