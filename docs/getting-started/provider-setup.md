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

Manual setup connects three settings:

- `.crewplane/config.yml` defines named provider profiles under `agents`.
- The workflow lists those same agent names under each node's `providers`.
- `settings.integrations.invoker.implementation` controls whether Crewplane
  generates mock output or runs the provider CLIs.

The names must match exactly. Set the invoker implementation to `cli` to enable
real provider commands.

![Provider setup diagram showing that `agents.codex` in `.crewplane/config.yml` must match `providers: ["codex"]` in the workflow, then the invoker changes from mock to cli before validation and execution.](../images/providers/provider-setup-two-files.png)

First, confirm the provider CLI is installed. For Codex, this command should
print its version:

```bash
codex --version
```

Sign in or configure credentials using the provider CLI before continuing.
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

In Crewplane, an `agent` is a named set of provider CLI settings. Workflow nodes
use that name to select the settings:

```yaml
agents:
  codex:
    cli_cmd: ["codex", "exec"]
    provider_kind: "codex"
    default_model: "gpt-6-astra"
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

With `implementation: "mock"`, Crewplane generates sample output without
starting provider CLIs. The `options` keys here control that sample output.

The generated agent named `mock` is used by the quickstart and onboarding demo.
You can remove it once no workflow references `providers: ["mock"]`; the
invoker setting still controls whether other agents use mock output.

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
configured under `agents`. Keep `options: {}`; the built-in CLI invoker does
not accept additional options.

## Choose A Provider Kind

`provider_kind` tells Crewplane which provider CLI you use. Crewplane uses it to
choose command options, read answers and usage reports, recognize usage limits,
and format logs. Install the provider CLI and configure its credentials separately.

Supported values:

- `claude`
- `codex`
- `copilot`
- `gemini`
- `kilo`
- `pi`
- `deepseek`
- `generic`

Use `generic` for a CLI without a dedicated provider kind.

Check that each provider CLI you plan to use is installed. For example:

```bash
claude --version
codex --version
gemini --version
copilot version
```

## Pi Text Mode

Install Pi and configure its credentials before using it with Crewplane.
The generated `.crewplane/config.yml` includes this commented profile.
Uncomment it and use `pi` in your workflow's `providers` list:

```yaml
agents:
  pi:
    cli_cmd: [pi]
    provider_kind: pi
    prompt_transport: stdin
    # Example for Pi's Codex login; choose your authenticated provider/model.
    # default_model: "openai-codex/gpt-5.5"
    extra_args: ["--no-extensions"]
    invocation_timeout_seconds: null
    invocation_idle_timeout_seconds: null
```

Keep `prompt_transport: stdin` and leave out `prompt_transport_arg`. Crewplane
sends the prompt and adds the options needed for Pi's text mode automatically.

Pi can use models from multiple providers. Set `default_model` to a
`provider/model` value matching the provider you authenticated with in Pi.
For a Pi Codex login, uncomment `default_model: "openai-codex/gpt-5.5"` above.
Use `pi --list-models` to find model IDs available in your installation.

A workflow's `model` takes precedence over the agent's `default_model`.
If neither is set, Pi chooses using its own configuration and available
credentials. Leave out workflow `reasoning`; this profile does not support it.

Crewplane runs Pi without saving a session and tells it to trust project
resources. The generated profile disables automatic extension loading so
installed extensions cannot introduce approval prompts. Pi's built-in file and
shell tools remain available. Extensions load only if you explicitly add them
with `--extension`; choose extensions that work without user input.

Pi returns its answer when the command finishes. Crewplane therefore ignores
`invocation_idle_timeout_seconds` for Pi.

Crewplane treats a failed command or blank answer as an error. This mode does
not report token usage; Crewplane shows an estimate based on the text it captures.

## DeepSeek Headless Mode

The DeepSeek profile runs `dsh` in headless mode, without an interactive
interface. Install and configure `dsh` before using it with Crewplane.
Onboarding checks that `dsh` and the `env` command are available.

Setup requires changes in both Crewplane and DeepSeek.

### Crewplane Configuration

Add or uncomment this profile in `.crewplane/config.yml` and use `deepseek` in
your workflow's `providers` list:

```yaml
agents:
  deepseek:
    cli_cmd: [env, DSH_PERMISSION_MODE=danger-full-access, dsh, --profile, headless]
    provider_kind: deepseek
    prompt_transport: argv
    prompt_transport_arg: "--"
    invocation_timeout_seconds: null
    invocation_idle_timeout_seconds: 1800
```

Keep the command and prompt settings shown above. Crewplane supplies the
prompt automatically. Choose the model and configure credentials in DeepSeek
itself. Leave out Crewplane's `default_model`, workflow `model`, and workflow
`reasoning` fields for this profile.

### DeepSeek Permissions

This profile requires DeepSeek's `danger-full-access` permission preset. It
removes DeepSeek's file sandbox and lets ordinary file and shell operations
run without asking for approval. Any request that still needs approval is
rejected instead of prompting.

Set this preset in `~/.dsh/settings.yaml`. If you set the `DSH_HOME` environment
variable to another directory, edit `settings.yaml` in that directory instead:

```yaml
permission:
  defaultPreset: danger-full-access
```

DeepSeek's saved preset takes priority over the `DSH_PERMISSION_MODE` setting in
the Crewplane command, so configure both. Crewplane checks the command but does
not check DeepSeek's saved permissions. See
[DeepSeek's settings guide](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/packages/settings/settings-file/README.md)
for more about its settings file.

## Choose A Model

Set `default_model` in an agent profile to choose its model. If you leave it
out, the provider CLI uses its own default.

To override the model for one workflow node, use a provider object:

```yaml
providers:
  - provider: codex
    model: gpt-5.3
```

Crewplane passes a workflow's `model` value to the provider CLI. DeepSeek is the
exception: leave out both model fields and choose its model in DeepSeek's own
settings.

Use `model_arg` only with `provider_kind: generic` to choose the model flag; it
defaults to `--model`. Set it to `null` to send no model flag. For other provider
kinds, the built-in CLI invoker ignores `model_arg` and warns if you set it.

## Choose Reasoning

For Codex and Claude, you can use `reasoning` to request how much effort the
provider spends on a task. This requires the built-in `cli` invoker and the
matching `provider_kind`. Use a value supported by your chosen provider and model:

```yaml
providers:
  - provider: codex
    model: gpt-5.6-sol
    reasoning: xhigh
```

Crewplane records the value it sends; the provider decides how to apply it.
Leave out `reasoning` to use the provider's existing defaults and settings.

When a workflow sets `reasoning`, remove any Codex `model_reasoning_effort` or
Claude `--effort` options from `cli_cmd` and `extra_args` to avoid conflicting
settings. For Claude, also remove any non-empty `CLAUDE_CODE_EFFORT_LEVEL`
environment setting. Claude's `--settings` JSON or files may contain other
options, but must not also set `effortLevel` or `env.CLAUDE_CODE_EFFORT_LEVEL`.

Crewplane must be able to read those Claude settings during `crewplane validate`
to check for conflicts. When using `env` with a workflow reasoning request,
omit `--chdir` and `-C`: they change how relative settings paths are read.

## Choose Prompt Transport

`prompt_transport` controls how Crewplane sends the full workflow prompt to a
provider CLI:

- `stdin`: send the prompt directly to the running CLI through standard input.
- `argv`: include the prompt as an argument in the command that starts the CLI.

Keep the prompt settings from your provider's example. Pi requires `stdin`;
DeepSeek requires `argv`. For other CLIs, prefer `stdin` when supported to keep
prompts out of the command line.

These examples show both formats. Replace `provider-cli` with your CLI's command:

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

In `stdin` mode, set `prompt_transport_arg` only if the CLI needs an argument to
read from standard input. For example, Codex uses `prompt_transport_arg: "-"`.

In `argv` mode, `prompt_transport_arg` is required. Set it to the argument the
CLI expects before the prompt, such as `--prompt` in the example above.
Crewplane adds that argument and the full prompt automatically.

Prompts sent through `argv` may be visible to tools that list running processes
or record command arguments. Crewplane warns about this before the run. Very
long prompts can also exceed the operating system's command-length limit and
prevent the provider command from starting.

## Tune Retries, Quota, And Timeouts

Set retry delays and timeouts under `agents.<name>`. The same section controls
how Crewplane waits for provider usage limits to reset:

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

**Configured error retries** and **quota retries** are separate:

- **Configured error retries** use `retry_on_exit_codes`, `retry_on_stderr_contains`, and
  `retry_on_output_contains`. They only run when `max_retries` is greater than
  `0`; each retry waits `retry_delay_seconds`.
- **Quota retries** start when Crewplane recognizes a provider usage limit
  or finds one of your `quota_reached_on_contains` strings in the output. These
  retries are not capped by `max_retries`. Their five-hour time limit starts
  with the first response reporting a usage limit.
- If Crewplane can parse a provider reset time, it waits until that reset plus
  `quota_reset_sleep_floor_seconds`, but never less than
  `quota_reached_retry_delay_seconds`.
- Crewplane stops if the provider reports a reset more than five hours away, or
  if the retry period has ended or the next wait would reach its end.

> ⚠️ **Wall-clock timeout is a hard kill switch.**
> Leave `invocation_timeout_seconds` as `null` unless you explicitly want
> Crewplane to terminate a provider CLI after a fixed amount of elapsed time.
> For quiet or stalled processes, prefer `invocation_idle_timeout_seconds`; it
> cancels only after the provider stops producing output for that interval.
> Gemini's machine-readable JSON response is emitted only after completion, so
> Crewplane cannot enforce an output-idle timeout for Gemini invocations. Use
> `invocation_timeout_seconds` when those invocations need a hard limit.

See the [configuration reference](../reference/configuration.md) for every
config field.

## Next

After provider setup, start the real provider run:

```bash
crewplane run
```

`crewplane run` checks the workflow and config before starting provider CLIs.
If those checks fail, no provider command starts.

Continue to [Running workflows](../guides/running-workflows.md) to run the
configured workflow and learn about validation, resuming interrupted runs,
skipping completed work, and rerunning workflows.

Or browse the [Guides](../index.md#guided-tutorial-track).
