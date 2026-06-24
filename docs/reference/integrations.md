# Integrations Reference

Integrations are configured under `settings.integrations`.
Generated projects select the `mock` invoker by default.

Built-in aliases:

| Kind | Aliases |
| --- | --- |
| `invoker` | `cli`, `mock` |
| `ui` | `tmux`, `none` |
| `artifacts` | `filesystem` |

Aliases resolve through the internal registry. An external adapter can also be
selected with `package.module:ClassName` or `package.module.ClassName`.

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

The `tmux` UI opens a compact live dashboard when output is a terminal,
`--no-live` is not set, tmux is available, and provider CLI output logging is
enabled. Missing tmux degrades to a warning and normal execution.

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
preflight bundles, locks, and workspace state under `.crewplane/`.

Options:

- `log_cli_output`
- `allowed_template_paths`

This is the only built-in artifact backend. Real execution relies on the
artifact-store port for locks, skip/resume history, full-run output, and
workspace lineage; a custom backend must implement those port capabilities to be
usable for real runs.
