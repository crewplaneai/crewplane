# Observability

Crewplane records run events, provider logs, and summaries for real execution.
The default UI integration is the tmux live dashboard.

## Live Dashboard

By default, `settings.integrations.ui.implementation` is `tmux`. When tmux is
available, real runs open or switch to a compact live dashboard.

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

If tmux is missing, Crewplane warns and continues without the dashboard. Use
`orchestrator run --no-live` to disable live dashboard output explicitly.

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
.orchestrator/execution-stages/<run-key>/logs/summary.md
.orchestrator/execution-stages/<run-key>/logs/events.ndjson
```

Summaries include run status and visible-text usage/spend estimates when the
provider output contains enough information or pricing is configured.
