# Observability

Crewplane records run events, provider logs, and summaries for real execution.
The default UI integration is the tmux live dashboard.

## What You Can Observe

| Need | Where to look |
| --- | --- |
| Live DAG status | tmux dashboard. |
| Run summary | `.crewplane/execution-stages/<run-key>/logs/summary.md`. |
| Event timeline | `.crewplane/execution-stages/<run-key>/logs/events.ndjson`. |
| Provider output | Node-local provider logs. |
| Final results | `.crewplane/execution-results/<run-key>/`. |

tmux is optional. The quickstart uses `crewplane run --no-live`, so tmux is not
required for the first successful run. Logs and summaries are still written
under `.crewplane/execution-stages/<run-key>/logs/`.

## Live Dashboard

By default, `settings.integrations.ui.implementation` is `tmux`. Real runs open
or switch to a compact live dashboard only when output is attached to a terminal,
`--no-live` is not set, tmux is available, and provider log capture is enabled.

```yaml
settings:
  integrations:
    ui:
      implementation: "tmux"
      options:
        auto_close_session: true
        tmux_executable: "tmux"
        quiet_after_seconds: 120.0
        log_tail_lines: null
```

If tmux is missing, Crewplane warns and continues without the dashboard. CI and
other non-TTY runs still write `logs/events.ndjson` and `logs/summary.md`, but
do not start the live dashboard. Use `crewplane run --no-live` to disable live
dashboard output explicitly.

## Log Capture Dependency

The tmux UI requires provider CLI output logs. The default filesystem artifact
options enable this:

```yaml
settings:
  integrations:
    artifacts:
      implementation: "filesystem"
      options:
        log_cli_output: true
```

If logs are disabled, the live UI cannot show provider log tails.

## Inspecting Logs

Provider logs are persisted under each node stage directory. The tmux dashboard
can open formatted inspect when valid provider metadata exists and raw inspect
for the exact persisted `.log` file.

Run summaries are written to:

```text
.crewplane/execution-stages/<run-key>/logs/summary.md
.crewplane/execution-stages/<run-key>/logs/events.ndjson
```

Summaries include run status and visible-text usage/spend estimates when the
provider output contains enough information or pricing is configured.

See also:

- [Inspecting Run Records](inspecting-artifacts.md)
- [Running workflows](running-workflows.md)
