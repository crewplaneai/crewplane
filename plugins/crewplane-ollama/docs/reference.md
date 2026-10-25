# Ollama Plugin Reference

This reference covers invocation, tools, resource limits, and execution behavior.
Start with the [setup guide](setup.md) to configure the plugin.

The plugin exports `crewplane_ollama:OLLAMA`, registered explicitly under
`settings.integrations.invoker.options.providers.ollama` with distribution
`crewplane-ollama` and `provider_kind: ollama`.

## Command invocation

```text
crewplane-ollama-agent --model MODEL
crewplane-ollama-agent --check --model MODEL
```

The capability manages both flags. Configure `cli_cmd` as **one executable**,
including an absolute/custom path if needed; set `extra_args: []`,
`prompt_transport: stdin`, and a finite positive `invocation_timeout_seconds`.
The workflow model overrides `default_model`; the effective model must be
nonblank and not start with `-`. Omit `prompt_transport_arg` and first-class
`reasoning`. `model_arg` is ignored, as for other non-generic capabilities.
Executable lookup uses the runtime-prepared cwd and environment, including its
`PATH`. No user-defined command tail or extra flags are accepted.

Plugin 0.2 rejects old `ollama run` profiles and flags. Follow the
[migration steps](setup.md#upgrade-from-plugin-01) before using an existing profile.

Execution reads one bounded UTF-8 prompt from stdin. Only a normal, completed,
nonblank final assistant response is written to stdout. Intermediate content,
reasoning, tool results, malformed/partial responses, and HTTP error bodies are
never canonical answers. A final blocker report is valid protocol completion,
not proof that the requested work or checks succeeded. Existing reviewer and
findings output contracts still apply; the wrapper does not rewrite them.

Non-streamed inference can be quiet for a long time. Output-idle timeout is
unsupported and disabled; set `invocation_idle_timeout_seconds: null` and use
the required whole-invocation deadline (onboarding uses 900 seconds). The wrapper
does not emit heartbeat output. Usage is unknown, logs use plain presentation,
and quota classification is always false, including for prose containing
“quota” and configured quota patterns.

## Native HTTP and readiness

The target API baseline is
[Ollama v0.40.2](https://github.com/ollama/ollama/blob/v0.40.2/api/types.go).
The wrapper uses `/api/chat`, with `stream: false`, `truncate: false`, and
`shift: false`; it retains the full conversation, native tool calls, optional
IDs, accompanying assistant content, and optional thinking in memory. It does
not set model-specific thinking controls or trim/compact history.

An explicit `OLLAMA_HOST` is authoritative. Without it, execution uses
`http://127.0.0.1:11434`; it never discovers or substitutes another endpoint.
Only HTTP(S) hosts with optional ports are accepted, without credentials,
non-root paths, queries, or fragments. Proxy environment variables are ignored,
redirects are not followed, and TLS verification remains enabled. Connections
have a finite timeout; slow inference reads are bounded by Crewplane's outer
invocation deadline. HTTP requests are never automatically retried.

Discovery, readiness, and execution startup all require `/api/show` metadata
with both `completion` and `tools`, excluding `remote_model`/`remote_host`
records. Startup rechecks eligibility before sending the repository prompt.
Metadata is not proof of a model's coding or instruction-following reliability.

`--check` checks eligibility and sends `Reply with OK only.` **without tools**;
it rejects tool calls and requires a normal nonblank answer. Availability uses
one supervised check with the effective workflow model, endpoint, cwd, and
configured deadline. Probe captures are deleted, and cancellation awaits shared
process cleanup. Real `validate`/`run` checks precede locks, dedupe, and resume;
structural validation, canonicalization, and dry-run are probe-free. Readiness
may load the model, but does not execute commands or create run artifacts.
Nothing pulls models, installs software, starts servers, or falls back to cloud
inference or text-only execution.

## Command tool

Exactly one tool is exposed:

```json
{
  "type": "function",
  "function": {
    "name": "run_command",
    "description": "Run a foreground command in the supplied workspace. Use an explicit interpreter or shell when needed. Obey the task's read-only and other restrictions.",
    "parameters": {
      "type": "object",
      "properties": {
        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "stdin": {"type": "string"}
      },
      "required": ["argv"],
      "additionalProperties": false
    }
  }
}
```

Arguments are validated locally: `argv` is a nonempty string array with a
nonblank executable, optional `stdin` is a string, NULs and extra properties are
rejected. There is no command-string, Markdown, or heuristic repair fallback.
Unknown tools and schema-invalid argument objects receive explicit tool errors
and consume an attempt. A malformed envelope or non-object arguments anywhere
invalidates the entire batch before any command in that batch runs.

Commands run serially with `shell=False`, inheriting the exact prepared cwd and
environment, including controlled Git variables. The model cannot choose a
separate cwd/environment through tool arguments. Explicit shells/interpreters
are allowed when the task permits them; an argument vector is not a sandbox.
On Windows, direct `.bat`/`.cmd` execution, including resolved batch files, is
rejected; native executables or explicitly invoked interpreters are the intended
contract. Native Windows execution acceptance remains pending.

Stdin, stdout, and stderr are handled concurrently. EOF is supplied without
input. JSON feedback contains `exit_code`, `stdout`, `stderr`, `stdout_bytes`,
`stderr_bytes`, `stdout_truncated`, and `stderr_truncated`. Invalid UTF-8 output
is decoded with replacement. Launch failures have an error and no exit code;
ordinary nonzero exits remain feedback the model can address within the same
conversation.

Commands inherit Crewplane's owned process tree: no new session, process group,
or breakaway job is created. Each direct child and its captured pipes must
complete before another call. Pipes held open after child exit cause fatal
failure after a short bounded drain grace; the runtime handles the owned tree.
Only foreground commands are supported. Background servers, daemons, and
deliberately detached children are outside this contract, not securely contained.

## Initial resource limits

| Resource | Bound |
| --- | --- |
| UTF-8 stdin prompt | 1 MiB, rejected before inference |
| Each HTTP response | 2 MiB, enforced while consuming the body |
| Serialized request, including complete history | 8 MiB before sending |
| Tool attempts | 64, including unknown tools and invalid arguments |
| Retained command stdout / stderr | 64 KiB each, split between head and tail; all discarded bytes still drained and counted |
| Whole invocation | Required finite positive `invocation_timeout_seconds` |

An over-budget batch executes none of its calls. After attempt 64, one final
answer request is allowed, but additional calls fail: at most 65 chat requests.
No partial success, silent history truncation, or context compaction is used.
These are initial safety/resource bounds, not performance guarantees or proof
that the server has enough model context. Configure context on the server as
explained in [setup](setup.md#model-context).

## Explicit skills and roles

Name an actual `SKILL.md` path in the prompt, or include it with `{{file:...}}`
and state its source directory. The model is instructed to read applicable
root/scoped `AGENTS.md` files and the selected skill, resolve linked resources
and scripts relative to the skill directory, and retain runtime cwd/environment.
Reviewer/read-only restrictions remain authoritative; tool availability does
not authorize edits or make read-only access enforceable.

Skills/resources must exist in the prepared workspace or an explicitly supplied
readable location. Managed worktrees/snapshots contain tracked source files, not
ignored caller skills or another agent's private installation. Existing
[template path restrictions](../../../docs/reference/workflow-syntax.md#templates)
are unchanged. Missing required files, scripts, interpreters, MCP tools, or host
APIs must be reported as concrete blockers, stopping affected work; an executable
alternative is appropriate only when the task/skill permits it. There is no skill
registry, automatic discovery or installation, frontmatter permission engine,
MCP integration, or host API bridge.

## Authority, lifecycle, and evidence

**Model-selected commands run on the Crewplane host with your permissions.**
They can reach files, credentials, and networks outside the workspace. The
selected server receives repository prompts and tool context. Worktrees,
snapshots, template allowlists, and argument vectors are **not sandboxes**;
external isolation is your responsibility. WSL commands run in WSL even when
the model is served on Windows. Onboarding discloses the selected endpoint and
command privileges before saving an enabled profile.

Bounded tool-start/result and protocol diagnostics go to stderr; reasoning is
not automatically logged and the environment is not dumped. Provider-log
capture controls durability, not execution. Command and final output may contain
secrets, and raw provider logs are not guaranteed redacted. Existing
`.crewplane/execution-stages/` and `.crewplane/execution-results/` locations are
unchanged; see [run records](../../../docs/guides/inspecting-artifacts.md).

The default is `max_retries: 0`. Configured provider retries restart the whole
conversation, not an individual command. Worktree retries reset selected source
and rerun setup; snapshots rematerialize. Project-root edits are not rolled back,
and no workspace mode rolls back external effects. Retries can repeat effects.
Timeout/cancellation ends the provider attempt and its owned descendants;
publication/retry waits for runtime cleanup. The independently managed Ollama
server remains running and may continue GPU work.

No persistent conversation or tool checkpoint is saved. Only existing verified
completed nodes/review phases can be reused; interrupted loops restart under the
normal checkpoint policy. Existing signatures include command, model, explicit
environment, timeout/settings, and plugin distribution version. Replacing model
weights behind an unchanged tag is outside that identity; use `--force` when
appropriate.

Linux, macOS, and WSL follow Crewplane's supported scope. Native Windows requires
its existing launcher/descendant-cleanup acceptance; simulated tests are not
acceptance. Deterministic scripted-server tests establish protocol and real
command effects, not model compliance. Live acceptance must separately record
server/model identity, actual context allocation, selected skill/resources,
failed-test correction, independently rerun checks, and unavailable-capability
blocker behavior.
