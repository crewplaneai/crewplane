# Integrations Reference

Integrations are configured under `settings.integrations`.

Built-in aliases:

| Kind | Aliases |
| --- | --- |
| `invoker` | `cli`, `mock` |
| `ui` | `tmux`, `none` |
| `artifacts` | `filesystem` |

Aliases resolve through the internal registry. A dotted path can also be used
for an external adapter, for example `package.module:ClassName`.

## Invokers

### `cli`

The `cli` invoker runs configured provider commands from `agents`. It owns
prompt transport, retries, quota detection, timeout handling, output extraction,
and provider usage parsing.

Options: none.

### `mock`

The `mock` invoker provides deterministic provider-free execution.

Options:

- `delay_seconds`
- `observation_delay_seconds`
- `output_mode`
- `output_dir`
- `strict_file_mode`
- `seed`
- `fail_when`

`output_mode` is one of `lorem`, `echo`, and `file`. `fail_when[]` selectors
support `node_id`, `task_id`, `provider`, `role`, `audit_round_num`, and
`round_num`.

## UI

### `tmux`

The `tmux` UI opens a compact live dashboard when tmux is available. Missing
tmux degrades to a warning and normal execution.

Options:

- `auto_close_session`
- `tmux_executable`
- `quiet_after_seconds`
- `log_tail_lines`

### `none`

The `none` UI disables live observers.

Options: none.

## Artifacts

### `filesystem`

The `filesystem` artifact backend stores stages, results, logs, manifests,
preflight bundles, locks, and workspace state under `.orchestrator/`.

Options:

- `log_cli_output`
- `allowed_template_paths`

Real execution currently depends on this backend for lock, skip, resume,
full-run, and workspace-lineage behavior. Non-filesystem artifact backends are
limited to validation and dry-run in this release.
