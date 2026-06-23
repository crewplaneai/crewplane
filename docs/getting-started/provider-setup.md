# Provider Setup

Crewplane invokes provider CLIs from your machine. It does not manage provider
accounts, API keys, approvals, or sandbox settings.

## Agent Config

Agents are configured in `.orchestrator/config.yml`:

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

Workflow nodes reference agents by name:

```yaml
nodes:
  - id: implement
    mode: parallel
    providers: ["codex"]
```

The provider name in a workflow must exist under `agents`.

## Provider Kinds

`provider_kind` can be one of:

- `claude`
- `codex`
- `copilot`
- `gemini`
- `kilo`
- `generic`

Provider kind lets Crewplane apply provider-aware output and usage handling at
the invoker boundary. It does not install or authenticate the provider tool.

## Models

`default_model` is optional. If omitted, the provider CLI chooses its configured
default. A workflow provider object can override the model for one node:

```yaml
providers:
  - provider: codex
    model: gpt-5.4
```

When a model is supplied, `model_arg` controls the CLI flag used to pass it. The
default is `--model`; set `model_arg: null` for a CLI that does not accept a
model flag through this path.

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

When `prompt_transport: "argv"` is used, `prompt_transport_arg` is required.
Preflight emits a warning because argv prompts can be visible in process lists
or shell histories depending on the platform and tooling.

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
