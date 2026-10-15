# Crewplane Ollama

Generate text with an operator-provisioned local Ollama model inside a Crewplane
workflow, then pass the answer to downstream nodes through ordinary artifacts.

Install this distribution alongside the Crewplane wheel in the same Python 3.13+
environment. See [provider setup](../../docs/getting-started/provider-setup.md#local-ollama-text-generation)
for pip/uv installation, the `local` agent profile, server configuration, and the
bundled example. This package exports `crewplane_ollama:OLLAMA`; it is loaded only
when registered under `settings.integrations.invoker.options.providers.ollama`.

This package requires Crewplane 0.3.8 or newer, the first assigned release with
the public capability contract. Until that release is published, install the
paired source-built wheels from this checkout. Live Ollama and macOS acceptance
remain required before release.

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
inference under one 60-second deadline. Both use the fully resolved executable,
the same server environment, and the fixed prompt `Reply with OK only.`. Probe
captures are deleted after consumption; cancellation awaits shared process
cleanup. Dry-run does not probe.

Provisioning, server cloud policy, model-removal pull races, resource consumption,
CLI-only cancellation, separate concurrency limits, and unchanged-version/tag
identity limits are explained in the [setup guide](../../docs/getting-started/provider-setup.md#local-ollama-text-generation).
Linux/macOS/WSL follow Crewplane's platform scope; native Windows acceptance is
pending. Live Ollama and macOS startup behavior require the documented manual
acceptance before release.
