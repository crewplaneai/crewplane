# Provider Setup

Use this page after you finish the [Quickstart](quickstart.md) and want the
generated project to run multiple real providers. By then,
you have initialized `.crewplane/`, validated the generated workflow, run the
mock workflow, and inspected the local run record.

Provider setup is where you leave mock mode. Real provider runs start the
external CLI commands configured in `.crewplane/config.yml`, and those tools keep
their own filesystem, network, credential, approval, and sandbox settings.
Crewplane coordinates the workflow and writes the run record; it does not
sandbox provider CLIs.

Crewplane also does not install provider CLIs, manage provider credentials,
restrict provider network access, or guarantee that provider-generated content
is safe to execute.

If you want a quick setup for one real provider, start with the
`crewplane onboarding` command in the
[quickstart onboarding step](quickstart.md#5-onboard-a-provider).

## Connect One Provider Manually

Manual setup connects the workflow to provider profiles, then chooses the
invoker:

- `.crewplane/config.yml` defines named provider profiles under `agents`.
- The workflow lists those same agent names under each node's `providers`.
- `settings.integrations.invoker.implementation` decides whether those names use
  mock output or real CLI calls.

The names must match exactly. Setting the invoker implementation to `cli` is the
point where `crewplane run` can start the external provider commands listed
under `agents`.

![Provider setup diagram showing that `agents.codex` in `.crewplane/config.yml` must match `providers: ["codex"]` in the workflow, then the invoker changes from mock to cli before validation and execution.](../images/providers/provider-setup-two-files.png)

First, confirm the provider CLI works outside Crewplane:

```bash
codex --version
```

Then add or uncomment one real provider profile in `.crewplane/config.yml` and
switch the invoker to `cli`. A minimal Codex setup looks like this:

```yaml
version: "1.0"

agents:
  codex:
    cli_cmd: ["codex", "exec"]
    provider_kind: "codex"
    prompt_transport: "stdin"
    prompt_transport_arg: "-"

settings:
  integrations:
    invoker:
      implementation: "cli"
      options: {}
```

Replace the generated mock invoker options with `options: {}`. The `cli`
invoker does not accept the mock-only options generated for the first run.

Next, point the workflow node at the same agent name:

```yaml
nodes:
  - id: review.project
    mode: parallel
    providers: ["codex"]
```

Validate before you run:

```bash
crewplane validate
crewplane run
```

> You are leaving mock mode when you make this edit. From this point on,
> `crewplane run` can start the external provider commands configured under
> `agents`. Review provider CLI permissions, approval mode, sandbox settings,
> credentials, and network behavior before running.

## How Agent Names Work

In Crewplane, an `agent` is a named provider CLI configuration. It is not a
Python object or a long-running service. Workflow nodes reference agents by
name:

```yaml
agents:
  codex:
    cli_cmd: ["codex", "exec"]
    provider_kind: "codex"
    default_model: "gpt-5.5"
    prompt_transport: "stdin"
    prompt_transport_arg: "-"
    extra_args:
      - "--skip-git-repo-check"
```

```yaml
nodes:
  - id: implement
    mode: parallel
    providers: ["codex"]
```

The provider name in a workflow must exist under `agents`. Crewplane uses that
name to find the provider profile for each node.

## Turn Mock Mode On/Off

The generated `mock` agent is only for the quickstart and onboarding demo. You
can remove it once your workflows no longer reference `providers: ["mock"]`; it
is ***not*** what makes a run use mock output.

Mock mode is controlled by the invoker implementation:

```yaml
settings:
  integrations:
    invoker:
      implementation: "mock"
      options:
        output_mode: "lorem"
        seed: 42
        delay_seconds: 0.25
        observation_delay_seconds: 5
```

With `implementation: "mock"`, Crewplane writes deterministic mock output and
does not start provider CLIs. The `options` keys here belong to the mock
invoker.

To run real provider CLIs, switch the same setting to `cli` and remove those
mock-only options:

```yaml
settings:
  integrations:
    invoker:
      implementation: "cli"
      options: {}
```

With `implementation: "cli"`, `crewplane run` starts the external commands
configured under `agents`. Keep `options: {}` unless your chosen CLI invoker
configuration specifically needs additional options.

## Choose A Provider Kind

`provider_kind` tells the built-in CLI invoker which provider-aware behavior to
use at the invoker boundary. It can affect output extraction, quota parsing, log
formatting, and usage parsing. It does not install or authenticate the provider
tool.

Supported values:

- `claude`
- `codex`
- `copilot`
- `gemini`
- `kilo`
- `generic`

Confirm provider commands directly before running Crewplane with the `cli`
invoker:

```bash
claude --version
codex --version
gemini --version
copilot version
```

## Choose A Model

`default_model` is optional. If you omit it, the provider CLI chooses its
configured default.

To override the model for one workflow node, use a provider object:

```yaml
providers:
  - provider: codex
    model: gpt-5.3
```

When a workflow node supplies `model`, Crewplane passes that value to the
provider CLI. Built-in provider kinds choose their own model flag. For
`provider_kind: generic`, use `model_arg` to choose the flag; it defaults to
`--model`. Set `model_arg: null` if your generic CLI should not receive a model
flag.

## Choose Reasoning

Provider objects can request a provider-native reasoning value when the
built-in `cli` invoker uses `provider_kind: codex` or `provider_kind: claude`:

```yaml
providers:
  - provider: codex
    model: gpt-5.6-sol
    reasoning: xhigh
```

Crewplane passes Codex requests through
`--config model_reasoning_effort="..."` and Claude requests through
`--effort ...`. The value is provider-native and may be model-dependent;
Crewplane records the request but does not claim that it was applied
unchanged. Omit `reasoning` to leave the provider's current defaults and user
configuration unmanaged.

Do not configure a second reasoning authority in `cli_cmd` or `extra_args`.
For Claude, a non-empty inherited `CLAUDE_CODE_EFFORT_LEVEL` also conflicts.
Explicit `--settings` JSON or files may contain unrelated settings, but
`effortLevel` or `env.CLAUDE_CODE_EFFORT_LEVEL` conflicts with the workflow
field. When reasoning is requested, Crewplane must be able to read and parse
each explicit Claude settings source so `crewplane validate` can report
conflicts before launch. An `env` wrapper cannot use `--chdir` or `-C` with a
workflow reasoning request because it would change relative settings resolution.

## Choose Prompt Transport

Crewplane can send the rendered prompt to a provider CLI in two ways:

- `stdin`: pass the rendered prompt through standard input.
- `argv`: pass the rendered prompt as an argument after `prompt_transport_arg`.

Use `stdin` when the provider CLI supports it. It keeps long prompts out of the
command line and is the generated default for supported providers.

```yaml
agents:
  stdin_agent:
    cli_cmd: ["provider-cli"]
    prompt_transport: "stdin"

  argv_agent:
    cli_cmd: ["provider-cli"]
    prompt_transport: "argv"
    prompt_transport_arg: "--prompt"
```

In `stdin` mode, Crewplane sends the prompt on standard input. If
`prompt_transport_arg` is set, that token is appended by itself; this is useful
for CLIs that require a stdin sentinel such as `-`.

When `prompt_transport: "argv"` is used, `prompt_transport_arg` is required and
Crewplane appends both the flag and the rendered prompt. Preflight emits a
warning because argv prompts can be visible in process lists or shell histories
depending on the platform and tooling.

## Tune Retries, Quota, And Timeouts

Per-agent retry and quota behavior is configured under `agents.<name>`:

```yaml
agents:
  claude:
    cli_cmd: ["claude"]
    max_retries: 2
    retry_delay_seconds: 300
    retry_on_exit_codes: [1]
    retry_on_stderr_contains:
      - "temporarily unavailable"
    quota_reached_on_contains:
      - "usage limit reached"
    quota_reached_retry_delay_seconds: 300
    quota_reset_sleep_floor_seconds: 5
    invocation_timeout_seconds: null
    invocation_idle_timeout_seconds: 1800
```

**Generic retries** and **quota retries** are separate:

- **Generic retries** use `retry_on_exit_codes`, `retry_on_stderr_contains`, and
  `retry_on_output_contains`. They only run when `max_retries` is greater than
  `0`; each retry waits `retry_delay_seconds`.
- **Quota retries** start when provider output matches built-in quota detection or
  one of your `quota_reached_on_contains` strings. They are not limited by
  `max_retries`; Crewplane retries quota hits inside a five-hour guard window.
- If Crewplane can parse a provider reset time, it waits until that reset plus
  `quota_reset_sleep_floor_seconds`, but never less than
  `quota_reached_retry_delay_seconds`.
- Crewplane does not sleep past the five-hour guard.
  - It stops immediately when a provider reports a reset more than five hours away.
  - If earlier quota waits have already used part of the window, Crewplane also stops when the next wait would bring the same quota-retry sequence to five hours or more.

> ⚠️ **Wall-clock timeout is a hard kill switch.**
> Leave `invocation_timeout_seconds` as `null` unless you explicitly want
> Crewplane to terminate a provider CLI after a fixed amount of elapsed time.
> For quiet or stalled processes, prefer `invocation_idle_timeout_seconds`; it
> cancels only after the provider stops producing output for that interval.

See the [configuration reference](../reference/configuration.md) for every
config field.

## Next

After provider setup, start the real provider run:

```bash
crewplane run
```

`crewplane run` performs preflight validation before execution, so it stops
before starting provider CLIs if the workflow or config is invalid.

Continue to [Running workflows](../guides/running-workflows.md) to run the
configured provider workflow and understand preflight, resume, duplicate skips,
and reruns.

Or browse the [Guides](../index.md#guides).
