# Crewplane Ollama

Crewplane's official Ollama plugin generates text with a local model and passes
the answer to downstream workflow nodes through ordinary artifacts.

Install this distribution alongside Crewplane 0.3.8+ in the same Python 3.13+
environment. See [provider setup](../../docs/getting-started/provider-setup.md#local-ollama-text-generation)
for package installation, server configuration, and the bundled text-generation
example. Generated configs include a commented `local` agent and plugin
registration; enable both manually or follow the
[onboarding walkthrough](../../docs/getting-started/quickstart.md#5-onboard-a-provider).
This package exports `crewplane_ollama:OLLAMA`; it is loaded when explicitly
selected in onboarding or registered under
`settings.integrations.invoker.options.providers.ollama`.

## CLI contract

Targets [Ollama v0.40.2](https://github.com/ollama/ollama/blob/v0.40.2/cmd/cmd.go):

```text
ollama run MODEL --nowordwrap --hidethinking
```

The workflow model overrides the agent default. A nonblank model is required.
The rendered prompt is sent as UTF-8 stdin; `cli_cmd` must begin with the
configured executable and `run`. Both `cli_cmd` and `extra_args` are validated.
Allowed literal options are `--keepalive VALUE`, `--format VALUE`, `--verbose`,
`--insecure`, and `--think` (or `--think=true|false|high|medium|low`). Value options
also accept `=VALUE`; boolean options accept `=true`/`=false`. Competing positional
models/prompts, other options, managed flags, alternate prompt transport, and
first-class `reasoning` are rejected. `model_arg` is ignored. Arguments retain
their literal values and execution uses the runtime-supplied working directory.
Hiding thinking affects displayed output, not whether thinking is computed.

Zero exit status and nonblank stdout are required. Stderr remains diagnostics;
nonzero partial output, empty output, and stderr-only output fail. Logs use plain
presentation. Provider token counts remain unknown. Local-server quota handling
is disabled explicitly, including configured quota patterns, and failure
classification does not infer quota exhaustion from generated prose. Configured
ordinary retries, wall-clock timeouts, and output-idle timeouts still apply.

## Availability and limits

Validation and real-run preflight probe model presence followed by diagnostic
inference under one deadline from the agent's `invocation_timeout_seconds`, or
60 seconds when it is `null`. Both use the fully resolved executable,
the same server environment, and the fixed prompt `Reply with OK only.`. Probe
captures are deleted after consumption; cancellation awaits shared process
cleanup. Dry-run does not probe.

Provisioning, server cloud policy, model-removal pull races, resource consumption,
CLI-only cancellation, separate concurrency limits, and unchanged-version/tag
identity limits are explained in the [setup guide](../../docs/getting-started/provider-setup.md#local-ollama-text-generation).
Linux/macOS/WSL follow Crewplane's platform scope; native Windows acceptance is
pending.
