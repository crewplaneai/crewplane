# Ollama Plugin Reference

This reference covers behavior specific to the Ollama agent. For configuration
and migration, see the [setup guide](setup.md).

## Command invocation

```text
ollama-agent --model MODEL
ollama-agent --check --model MODEL
```

Crewplane manages the flags and sends the UTF-8 prompt on stdin. `cli_cmd` must
contain one executable, `extra_args` must be empty, and the model must be nonblank
and not start with `-`. Omit `prompt_transport_arg` and workflow `reasoning`.
A finite positive `invocation_timeout_seconds` is required. Output-idle timeout
is disabled because non-streamed inference can remain quiet until completion.

Only a complete, nonblank final answer goes to stdout. Command and protocol
diagnostics go to stderr; thinking and tool results are not emitted as answers.
Token usage is unknown, and quota-based retries are disabled.

## Native HTTP and readiness

The API baseline is [Ollama v0.40.2](https://github.com/ollama/ollama/blob/v0.40.2/api/types.go).
The agent uses `/api/chat` with `stream: false`, `truncate: false`, and
`shift: false`. It preserves the full conversation, including tool calls and
thinking, without automatic history trimming or compaction.

`OLLAMA_HOST` selects the server; the default is `http://127.0.0.1:11434`.
Execution never substitutes another endpoint. Only HTTP(S) hosts with optional
ports are accepted, without credentials, non-root paths, queries, or fragments.
Proxy variables are ignored, redirects are disabled, and TLS verification stays
enabled. HTTP requests are not automatically retried.

Discovery, readiness, and execution require `/api/show` metadata with both
`completion` and `tools`; records with `remote_model` or `remote_host` are rejected.
Execution rechecks eligibility before sending the repository prompt. `--check`
verifies eligibility and requests a short reply without tools; tool calls or an
abnormal or blank reply fail the check. See [validation](../../../docs/reference/commands.md#crewplane-validate)
for when Crewplane performs provider checks.

## Command tool

The agent exposes one tool, `run_command`:

| Argument | Requirement |
| --- | --- |
| `argv` | Required nonempty string array with a nonblank executable. |
| `stdin` | Optional string; omitted input closes stdin immediately. |

NUL characters and extra properties are rejected. Commands run serially with
`shell=False` in the prepared workspace and environment; the tool accepts no
cwd or environment overrides. Use an explicit shell or interpreter when needed.
Only foreground commands are supported. Pipes left open by descendants after
the direct child exits cause failure after a bounded drain period.

Results contain `exit_code`, `stdout`, `stderr`, `stdout_bytes`, `stderr_bytes`,
`stdout_truncated`, and `stderr_truncated`. Output is decoded as UTF-8 with
replacement. Launch errors have no exit code; ordinary nonzero exits are returned
to the model so it can correct the command within the same conversation.

Unknown tools and invalid argument objects return tool errors and consume an
attempt. Malformed call envelopes or non-object arguments fail the entire batch
before any command runs. The agent does not repair malformed calls.

Skills must be explicitly selected. The plugin provides no automatic skill
discovery, installation, or MCP/host API bridge. Standard
[template rules](../../../docs/reference/workflow-syntax.md#templates) apply.

## Resource limits

| Resource | Bound |
| --- | --- |
| UTF-8 stdin prompt | 1 MiB |
| Each HTTP response | 2 MiB |
| Serialized request, including conversation history | 8 MiB |
| Tool attempts, including unknown tools and invalid arguments | 64 |
| Retained stdout / stderr per command | 64 KiB each, split between head and tail; excess output is drained and counted |

An over-budget batch executes none of its calls. After attempt 64, one final
answer request is allowed; further tool calls fail. These byte limits are separate
from the [model context](setup.md#model-context) configured on the server.

## Execution lifecycle

Crewplane's standard [retry settings](../../../docs/reference/configuration.md#agentsname),
[workspace behavior](../../../docs/guides/workspace-isolation.md),
[process cleanup](../../../docs/reference/integrations.md#files-processes-and-cancellation),
[run records](../../../docs/guides/inspecting-artifacts.md), and
[resume rules](../../../docs/guides/running-workflows.md#resume) apply.

For this plugin:

- Provider retries restart the whole conversation and may repeat command effects;
  individual tool calls are not checkpoints.
- Cancellation stops the owned agent processes. The separately managed Ollama
  server remains running and may continue inference.
- Replacing model weights behind the same tag does not change Crewplane's run
  signature; use `--force` to avoid reusing results from the previous weights.
