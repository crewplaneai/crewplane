# Configuration Reference

Config lives at `.crewplane/config.yml` by default. The `version` field must
match the current `SCHEMA_VERSION` in `src/crewplane/version.py`.

Generated config from `crewplane init` starts with one active `mock` agent and
`settings.integrations.invoker.implementation: "mock"`. That makes the first
`crewplane validate` and `crewplane run --no-live` provider-free. Mock output is
deterministic scaffolding, not model output.

The generated config also includes commented provider examples for Claude,
Codex, Gemini, Copilot, and Kilo. Uncomment and review only the agents you need
before switching the invoker to `cli`.

## Top Level

| Field | Description |
| --- | --- |
| `version` | Config schema version. |
| `agents` | Mapping of provider names to `AgentConfig`. |
| `settings` | Optional runtime settings. If omitted, default settings are used. |

## `agents.<name>`

| Field | Description |
| --- | --- |
| `cli_cmd` | Non-empty argv list for the provider CLI. |
| `provider_kind` | `claude`, `codex`, `copilot`, `gemini`, `kilo`, or `generic`. Defaults to `generic`. |
| `default_model` | Optional model name used when a workflow provider does not override `model`. |
| `model_arg` | CLI flag for model selection when `provider_kind: generic`. Defaults to `--model`; can be `null`. Built-in provider kinds use adapter-owned model flags. |
| `prompt_transport` | `stdin` or `argv`. Defaults to `stdin`. |
| `prompt_transport_arg` | Required for `argv`; optional stdin sentinel for `stdin`. |
| `extra_args` | Additional argv tokens appended to the provider command. |
| `max_retries` | Maximum retry attempts. Defaults to `0`. |
| `retry_delay_seconds` | Delay between generic retries. Defaults to `300.0`. |
| `retry_on_exit_codes` | Exit codes that trigger retry. |
| `retry_on_stderr_contains` | Stderr substrings that trigger retry. |
| `retry_on_output_contains` | Combined output substrings that trigger retry. |
| `quota_reached_on_contains` | Output substrings treated as quota exhaustion. |
| `quota_reached_retry_delay_seconds` | Delay after quota detection. Defaults to `300.0`. |
| `quota_reset_sleep_floor_seconds` | Minimum sleep when a quota reset time is parsed. Defaults to `5.0`. |
| `invocation_timeout_seconds` | Optional wall-clock timeout. Defaults to `null`. |
| `invocation_idle_timeout_seconds` | Optional idle-output timeout. Defaults to `1800.0`. |
| `pricing` | Optional token pricing buckets. |

## `agents.<name>.pricing`

Pricing values are per million tokens.

| Field | Description |
| --- | --- |
| `input` | Input token price. |
| `cached_input` | Cached input token price. |
| `cache_write` | Cache write token price. |
| `output` | Output token price. |
| `reasoning` | Reasoning token price. |
| `total` | Total token price. Cannot be combined with bucket-specific pricing. |

## `settings`

| Field | Description |
| --- | --- |
| `settings.log_level` | Runtime log level string. Defaults to `info`. |
| `settings.sequential_consensus_on_exhaustion` | `continue` or `fatal`. Defaults to `continue`. |
| `settings.max_audit_rounds` | Maximum allowed node `audit_rounds`. Defaults to `5`. |
| `settings.max_concurrent_nodes` | Optional cap on concurrent ready nodes. |
| `settings.max_parallel_invocations` | Optional cap on provider invocations inside a parallel node. |
| `settings.token_budget` | Global token budget warning/failure thresholds. |
| `settings.workspace` | Experimental workspace isolation settings. |
| `settings.integrations` | Adapter implementation and option settings. |

## `settings.token_budget`

| Field | Description |
| --- | --- |
| `settings.token_budget.warn_threshold_chars` | Warning threshold for rendered context size. Defaults to `50000`. |
| `settings.token_budget.fail_threshold_chars` | Optional fail-fast threshold for rendered context size. |

## `settings.workspace`

Experimental workspace isolation is disabled by default. Missing
`settings.workspace` is equivalent to `settings.workspace.enabled: false`.
When it is disabled, workflows must not declare `worktrees`, and provider nodes
run from the project root. `settings.default_workspace` is not supported.

| Field | Description |
| --- | --- |
| `settings.workspace.enabled` | Enables Experimental workspace materialization when workflows select worktrees. Defaults to `false`. |
| `settings.workspace.cache_root` | Optional absolute workspace cache path when workspace isolation is enabled. If omitted, Crewplane uses the platform cache location. |
| `settings.workspace.cleanup_on_success` | Delete successful workspace cache entries. Defaults to `true`. |
| `settings.workspace.worktree_contract` | `blob_exact`, the initial fail-closed Git blob-byte contract. |
| `settings.workspace.clean_start` | `strict` or `tracked_only`. Defaults to `strict`. |
| `settings.workspace.setup_profiles` | Mapping of setup profile names to commands. |
| `settings.workspace.setup_profiles.<name>.run` | Non-empty list of argv command lists. |
| `settings.workspace.setup_timeout_seconds` | Setup command timeout. Defaults to `600.0`. |
| `settings.workspace.identity.include_cache_root` | Include cache root in workspace identity. Defaults to `false`. |
| `settings.workspace.max_concurrent_materializations` | Maximum workspace materializations at once. Defaults to `1`. |
| `settings.workspace.disk.warn_free_bytes` | Optional free-space warning threshold. |
| `settings.workspace.disk.fail_free_bytes` | Optional free-space failure threshold. Must not exceed `warn_free_bytes`. |

Workspace-enabled runs require a supported ordinary Git repository, POSIX or WSL
filesystem behavior, and an invoker adapter that honors runtime-supplied `cwd`.
The initial `blob_exact` contract rejects Git LFS, custom filters,
byte-transforming text/eol attributes, submodules, sparse checkout, and partial
clone before provider invocation.

## `settings.integrations`

| Field | Description |
| --- | --- |
| `settings.integrations.invoker.implementation` | Invoker alias or dotted path. Defaults to `cli`. |
| `settings.integrations.invoker.options` | JSON-compatible invoker options. |
| `settings.integrations.ui.implementation` | UI alias or dotted path. Defaults to `tmux`. |
| `settings.integrations.ui.options` | JSON-compatible UI options. |
| `settings.integrations.artifacts.implementation` | Artifact alias or dotted path. Defaults to `filesystem`. |
| `settings.integrations.artifacts.options` | JSON-compatible artifact options. |

## Built-In Integration Options

### `cli` invoker

Runs configured `agents` commands against real provider CLIs instead of the
deterministic `mock` invoker. It has no options.

Real provider runs start the external commands configured in
`.crewplane/config.yml`. Those tools run with their own filesystem, network,
credential, approval, and sandbox settings. Crewplane coordinates them and
records artifacts; it does not sandbox them.

### `mock` invoker

| Option | Description |
| --- | --- |
| `delay_seconds` | Non-negative delay before mock output. Defaults to `0`. |
| `observation_delay_seconds` | Non-negative visible-observation delay. Defaults to `5.0`. |
| `output_mode` | `lorem`, `echo`, or `file`. Defaults to `lorem`. |
| `output_dir` | Fixture directory. Required when `output_mode: "file"`. |
| `strict_file_mode` | Fail on missing fixtures instead of fallback output. Defaults to `false`. |
| `seed` | Optional integer seed or `null`. |
| `fail_when` | List of failure selectors. |

`mock.fail_when[]` selector keys are `node_id`, `task_id`, `provider`, `role`,
`audit_round_num`, and `round_num`.

The generated mock agent uses a sentinel `cli_cmd` value. The mock invoker does
not start or validate that command.

### `tmux` UI

| Option | Description |
| --- | --- |
| `auto_close_session` | Close the tmux session at run end. Defaults to `true`. |
| `tmux_executable` | tmux executable name. Defaults to `tmux`. |
| `quiet_after_seconds` | Quiet-state threshold. Defaults to `120.0`; must be at least `1.0`. |
| `log_tail_lines` | Optional fixed log tail line count from `1` to `200`, or `null`. |

### `none` UI

Disables the live UI adapter. Runs still execute normally and write artifacts and
logs, but no tmux dashboard or other live observers are created. It has no
options.

### `filesystem` artifacts

| Option | Description |
| --- | --- |
| `log_cli_output` | Capture provider CLI output logs. Defaults to `true`. |
| `allowed_template_paths` | Absolute external paths allowed for `{{file:...}}` templates. Defaults to `[]`. |

### Dotted-Path Adapters

External adapters can be selected with a dotted path. Their `options` payload
must be JSON-compatible and is validated by the adapter.
