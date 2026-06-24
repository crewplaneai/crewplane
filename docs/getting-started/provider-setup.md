# Provider Setup

Use this page after the mock quickstart succeeds. The first
`crewplane init && crewplane validate && crewplane run --no-live` path does not
need provider CLIs, API keys, or config edits.

Real provider runs start the external commands configured in
`.crewplane/config.yml`. Those tools run with their own filesystem, network,
credential, approval, and sandbox settings. Crewplane coordinates them and
records artifacts; it does not sandbox them.

## Agent Config

In Crewplane, an `agent` is a named provider CLI configuration. It is not a
Python object or a long-running service. Agents are configured in
`.crewplane/config.yml`:

```yaml
agents:
  codex:
    cli_cmd: ["codex", "exec"]
    provider_kind: "codex"
    default_model: "gpt-5.4"
    prompt_transport: "stdin"
    prompt_transport_arg: "-"
    extra_args:
      - "--skip-git-repo-check"
```

Workflow nodes reference those agent names:

```yaml
nodes:
  - id: implement
    mode: parallel
    providers: ["codex"]
```

The provider name in a workflow must exist under `agents`.

The generated config starts with one active mock agent and commented examples
for Claude, Codex, Gemini, Copilot, and Kilo:

```yaml
agents:
  mock:
    cli_cmd: ["__crewplane_mock_invoker_never_executes__"]

settings:
  integrations:
    invoker:
      implementation: "mock"
```

For real providers, uncomment the agent entries you need, adjust command flags
for your local provider setup, and switch the invoker to `cli`:

```yaml
settings:
  integrations:
    invoker:
      implementation: "cli"
      options: {}
```

## Provider Kinds

`provider_kind` can be one of:

- `claude`
- `codex`
- `copilot`
- `gemini`
- `kilo`
- `generic`

Provider kind lets the built-in CLI invoker choose provider-aware output
extraction, quota parsing, log formatting, and usage parsing at the invoker
boundary. It does not install or authenticate the provider tool.

Confirm provider commands directly before running Crewplane with `cli`:

```bash
claude --version
codex --version
gemini --version
copilot version
```

## Models

`default_model` is optional. If omitted, the provider CLI chooses its configured
default. A workflow provider object can override the model for one node:

```yaml
providers:
  - provider: codex
    model: gpt-5.4
```

When a model is supplied, `model_arg` controls the CLI flag used to pass it for
`provider_kind: generic`. Built-in provider kinds use their adapter-owned model
flag. The default is `--model`; set `model_arg: null` for a generic CLI that
does not accept a model flag through this path.

## Prompt Transport

Crewplane supports two prompt transport modes:

- `stdin`: pass the rendered prompt through standard input.
- `argv`: pass the rendered prompt as an argument after `prompt_transport_arg`.

Examples:

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
for CLIs that require a stdin sentinel such as `-`. When `prompt_transport:
"argv"` is used, `prompt_transport_arg` is required and Crewplane appends both
the flag and the rendered prompt. Preflight emits a warning because argv prompts
can be visible in process lists or shell histories depending on the platform and
tooling.

## Retries, Quota, And Timeouts

Per-agent retry and quota behavior is configured under `agents.<name>`:

```yaml
agents:
  claude:
    cli_cmd: ["claude"]
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

`invocation_timeout_seconds` is an optional wall-clock cap. The default is
`null`, so active provider CLIs are not killed by wall-clock time. The idle
timeout cancels quiet or stalled processes when it is set.

See the [configuration reference](../reference/configuration.md) for every
config field.
