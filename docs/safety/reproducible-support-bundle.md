# Reproducible Support Bundle

Collect the smallest bundle that lets another person reproduce or inspect the
run without access to your machine.

Do not share provider logs until you have reviewed them for secrets and private
source content.

## Minimal Bundle

- Exact command output from `crewplane validate` or `crewplane run --no-live`.
- `.crewplane/config.yml`.
- The workflow `.task.md` file and any imported workflow files.
- `.crewplane/execution-stages/<run-key>/logs/summary.md`.
- Relevant result files from `.crewplane/execution-results/<run-key>/`.

## Full Bundle

Add these when the minimal bundle is not enough:

- `.crewplane/execution-stages/<run-key>/logs/events.ndjson`.
- Relevant node log files from `.crewplane/execution-stages/<run-key>/<node-id>/logs/`.
- `crewplane --version`, Python version, OS, shell, and install method.
- Provider CLI names and versions when the run used the `cli` invoker.

## Copy-Paste Checklist

- [ ] Command output
- [ ] `.crewplane/config.yml`
- [ ] Workflow `.task.md` files
- [ ] `logs/summary.md`
- [ ] `logs/events.ndjson`
- [ ] Relevant result files
- [ ] Relevant provider logs after redaction
- [ ] Crewplane, Python, OS, shell, install method, and provider CLI versions

## Redact

Before sharing, remove or replace:

- API keys, tokens, cookies, and credentials.
- Private repository URLs and hostnames.
- Secrets printed by provider CLIs.
- Proprietary source snippets that are not needed for the failure.
- Absolute home-directory paths when they identify people or machines.

Keep structure and filenames intact when possible. Replacing a secret with
`<redacted>` is better than deleting the whole line because timestamps, event
order, and file paths often explain failures.

## Skip, Force, And Resume Context

When the issue involves skipped or resumed work, include:

- `.crewplane/execution-stages/<run-key>/manifests/run.json`
- any relevant `.crewplane/execution-stages/<run-key>/<node-id>/resume-source.json`
- the prior run key named in the manifest or terminal output, if available

If `crewplane run --force` changes the behavior, include output from both the
ordinary run and the forced run.
