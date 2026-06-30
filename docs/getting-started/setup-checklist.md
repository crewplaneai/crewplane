# First Run Checklist

Use this after `crewplane init`, `crewplane validate`, and
`crewplane run --no-live`.

## Mock Status

- [ ] `.crewplane/config.yml` has one active agent named `mock`.
- [ ] `settings.integrations.invoker.implementation` is `mock`.
- [ ] The run output says `Mock invoker active: no provider CLI commands will be started.`
- [ ] Result files contain `# Mock Invocation Output`.

Mock output is deterministic scaffolding. It proves the workflow path, not model
quality.

## Safety Status

- [ ] No provider CLI, API key, provider account, or config edit was needed for the
  first run.
- [ ] Real provider runs start external commands from `.crewplane/config.yml`.
- [ ] Those tools keep their own filesystem, network, credential, approval, and
  sandbox settings.
- [ ] Crewplane writes a run record; it does not sandbox provider execution.

## Run Record Status

Confirm the run wrote:

- [ ] `.crewplane/execution-stages/<run-key>/logs/summary.md`
- [ ] `.crewplane/execution-stages/<run-key>/logs/events.ndjson`
- [ ] `.crewplane/execution-results/<run-key>/review.project-result.md`

Use [Inspecting Run Records](../guides/inspecting-artifacts.md) for the
directory layout and common files.

## Live UI Status

The quickstart uses `--no-live`, so tmux is not required. Later, omit
`--no-live` when you want the live dashboard and confirm:

- [ ] tmux is installed and on `PATH`.
- [ ] `settings.integrations.ui.implementation` is `tmux`.
- [ ] `settings.integrations.artifacts.options.log_cli_output` is `true`.

If tmux is missing, Crewplane warns and continues without the dashboard.

## Provider Handoff Status

After the mock status, safety status, run record, and optional live UI checks
pass, run:

```bash
crewplane onboarding
```

Confirm:

- [ ] Detection checked provider executable names on `PATH` only.
- [ ] Onboarding did not authenticate providers, run version commands, check
  account/model readiness, or inspect provider-tool settings.
- [ ] Generated defaults were updated, or onboarding stopped and linked to
  provider setup because generated files had local edits.
- [ ] No real provider CLI was started by onboarding.

## Ready For Real Providers

Before manual or additional provider setup:

- [ ] Install and authenticate each provider CLI outside Crewplane.
- [ ] Confirm each command works directly from your shell.
- [ ] Add one `agents.<name>` entry per provider command.
- [ ] Change workflow `providers` to match those agent names.
- [ ] Read [provider setup](provider-setup.md) and
  [security and trust](../safety/security-and-trust.md).

## Next

If onboarding configured one provider, make the first real run with
`crewplane run --no-live`. For edited files, additional providers, or manual
setup, continue to [Provider setup](provider-setup.md). If any earlier section
does not pass, check [Troubleshooting](../safety/troubleshooting.md).
