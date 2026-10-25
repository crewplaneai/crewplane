# Set Up the Ollama Plugin

Use the included `crewplane-ollama-agent` to run workflows with an installed local
tool-capable model. This guide targets Ollama v0.40.2, Python 3.13+, and
Crewplane 0.3.8+. For the tool contract and execution limits, see the
[plugin reference](reference.md).

## Prepare the server

Prepare an Ollama server and install a model that advertises both `completion`
and `tools`. Cloud-backed model records are excluded. Crewplane does not install
Ollama, start servers, or pull models. Standard
[Crewplane installations](../../../docs/getting-started/installation.md) include
the plugin; a separate Ollama CLI is not needed on the Crewplane host to run it.

To keep inference local, disable cloud features on the server computer: set
`disable_ollama_cloud` to `true` in `~/.ollama/server.json` or server-side
`OLLAMA_NO_CLOUD=1`, then restart the server and check its logs. Setting a variable
only in the Crewplane agent environment does not reconfigure a running server.
See [Ollama's cloud settings](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features).

## Configure Crewplane

> **Command authority:** model-selected commands run on the Crewplane host with
> your permissions and can access reachable files, credentials, and networks.
> Repository prompts and tool results go to the selected server. Worktrees and
> template restrictions are not sandboxes; use external isolation when needed.

Run `crewplane onboarding` and select Ollama, a reachable server, and an installed
local tool-capable model. Before saving, onboarding shows the endpoint and command
privileges. It saves the absolute agent executable path and explicit endpoint.
Executable lookup checks the configured command or `PATH`, then Crewplane's
Python environment, including private environments used by isolated installs.

For projects already using real providers, onboarding offers connection setup
without changing workflows or the model and other settings of compatible
profiles. This connection-only path skips the model test; run validation after
saving. For an older profile, follow the [migration steps](#upgrade-from-plugin-01).

If discovery times out, use `crewplane onboarding --discovery-timeout 60` to give
each search up to 60 seconds. To choose a server before discovery, set
`OLLAMA_HOST` in the terminal. Once saved, `agents.local.environment.OLLAMA_HOST`
overrides the terminal value; update that setting when the server address changes.
Without an explicit endpoint, execution uses `http://127.0.0.1:11434` and does not
try other servers. See [WSL setup](#windows-hosted-ollama-from-wsl) for a server
running on Windows.

### Manual configuration

Merge this profile and registration into `.crewplane/config.yml`, replacing the
model placeholder and endpoint. If switching from `mock`, replace the mock
invoker options with the `cli` options below.

```yaml
agents:
  local:
    cli_cmd: [crewplane-ollama-agent]
    provider_kind: ollama
    default_model: "<installed-tool-capable-model>"
    prompt_transport: stdin
    environment:
      OLLAMA_HOST: "http://127.0.0.1:11434"
    extra_args: []
    invocation_timeout_seconds: 900
    invocation_idle_timeout_seconds: null
    max_retries: 0
settings:
  integrations:
    invoker:
      implementation: cli
      options:
        providers:
          ollama:
            implementation: crewplane_ollama:OLLAMA
            distribution: crewplane-ollama
```

The bare executable name requires it to be on `PATH`. Otherwise, use onboarding
or set `cli_cmd` to the installed executable's absolute path. Generated configs
contain this commented profile and registration but stay mock-only until you
enable a provider. `crewplane init` preserves existing files.

Use `providers: [local]` on workflow nodes. A workflow `model` overrides the agent
default. Keep the command, prompt, and timeout settings shown above; omit
`prompt_transport_arg` and workflow `reasoning`. Increase the invocation timeout
for slower hardware or model loading. Keep `max_retries: 0` unless repeating
command effects is acceptable; see the [retry and cancellation contract](reference.md#authority-lifecycle-and-evidence).

### Upgrade from plugin 0.1

**Plugin 0.2 rejects `cli_cmd: [ollama, run]` profiles.** It adds host command
execution, with no text-only compatibility mode or automatic migration. After
reviewing the privileges above, update the existing profile:

- Replace `cli_cmd` with the single `crewplane-ollama-agent` executable or its
  absolute path, and clear `extra_args` to `[]`.
- Use `prompt_transport: stdin`, remove `prompt_transport_arg`, set a finite
  positive `invocation_timeout_seconds`, and set `invocation_idle_timeout_seconds`
  to `null`.
- Choose an installed local tool-capable model and save its server explicitly
  in `environment.OLLAMA_HOST`. Keep the plugin registration shown above and
  remove any workflow `reasoning` setting for this provider.

Connection-only onboarding does not repair legacy commands, flags, or timeouts.
Reconfigure them before using it, then [validate the workflow](#check-and-run).

## Model context

Provision sufficient context **on the selected Ollama server**, through its app
settings or server-side `OLLAMA_CONTEXT_LENGTH`, then reload the model as needed.
Check the loaded model's `CONTEXT` using `ollama ps` against that same server (on
its host, or with a separately installed Ollama CLI and the selected
`OLLAMA_HOST`). Setting `OLLAMA_CONTEXT_LENGTH` only on the Crewplane agent does
not resize a running server. Ollama recommends at least **64,000 tokens** for
coding agents, subject to available memory; check the actual allocation. See
[Ollama context configuration](https://docs.ollama.com/context-length).

A short readiness response does not prove adequate context for task/skill
instructions, repository reads, tool results, and the final response. The agent
retains complete history without automatic compaction. If context is exhausted,
increase server context or reduce the task's inputs. The
[resource limits](reference.md#initial-resource-limits) bound application resources,
not model context. Choose workflow concurrency according to server memory;
Crewplane has no GPU scheduler.

## Check and run

Validate the selected model, then run the generated text example:

```bash
crewplane validate .crewplane/workflows/example-templates/text-generation-example.task.md
crewplane run --no-live --tasks .crewplane/workflows/example-templates/text-generation-example.task.md
```

Validation checks local completion/tool eligibility and sends a short prompt
without tools. It does not execute model-selected commands or save run artifacts,
but may load the model and use GPU resources. Success confirms that the model can
respond; a failed check prevents the workflow from starting. `run` repeats this
check; `--dry-run` skips it. See the [readiness contract](reference.md#native-http-and-readiness)
for failure and deadline behavior.

## Windows-hosted Ollama from WSL

Keep Ollama and its installed models on Windows. Install Crewplane in WSL;
its included HTTP agent needs no Linux Ollama CLI. Model-selected commands run
**inside WSL**, not on the Windows server. Native Windows Crewplane acceptance
remains pending.

With WSL's default networking, select the Windows server and check connectivity:

```bash
export OLLAMA_HOST="http://$(ip -4 route show default | awk '{print $3; exit}'):11434"
curl --noproxy '*' --fail --silent --show-error --connect-timeout 3 --max-time 5 "$OLLAMA_HOST/api/version"
```

The `curl` command should print the server version. For mirrored networking,
`http://127.0.0.1:11434` may be appropriate instead; see
[Microsoft's WSL networking guide](https://learn.microsoft.com/en-us/windows/wsl/networking).
If connection fails, configure the Windows server's listen address and firewall
for the WSL network, then restart Ollama. Limit access to that network; Crewplane
changes neither server nor firewall. See
[Windows environment setup](https://docs.ollama.com/faq#setting-environment-variables-on-windows).
Keep cloud features disabled and configure model context on the Windows server.

The Windows setting controls listening; WSL's setting selects the endpoint.
Update `agents.local.environment.OLLAMA_HOST` if already saved, because it
overrides the terminal value. Otherwise run onboarding from this terminal,
choose the eligible model, and save the explicit endpoint.
