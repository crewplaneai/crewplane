# Artifacts Reference

Crewplane writes project-local state under `.orchestrator/`.

## Root Layout

```text
.orchestrator/
  config.yml
  workflows/
  preflight/
    fingerprint.key
  locks/
  execution-stages/
  execution-results/
```

The output directories are hyphenated:

- `.orchestrator/execution-stages/`
- `.orchestrator/execution-results/`

## Stage Runs

Each real run allocates:

```text
.orchestrator/execution-stages/<run-key>/
.orchestrator/execution-results/<run-key>/
```

`<run-key>` is derived from workflow name and run ID.

Stage run contents can include:

```text
logs/events.ndjson
logs/summary.md
manifests/run.json
manifests/nodes/*.json
<node-id>/logs/<provider>/*.log
<node-id>/review-state/review-loop-status.json
<node-id>/workspace-state.json
<node-id>/workspace-setup/setup.log
<node-id>/workspace-setup/setup.json
<node-id>/workspace-bundles/*.bundle
<node-id>/resume-source.json
```

Exact files depend on node mode and enabled features. Workspace files are
present only for Experimental workspace isolation runs.

## Results

Consolidated node artifacts are written under the matching result directory:

```text
.orchestrator/execution-results/<run-key>/<node-id>-result.md
.orchestrator/execution-results/<run-key>/<node-id>-findings.md
```

Findings files are present for nodes that declare `findings: true`.

## Preflight Files

Preflight compiles static resources, render plans, dependency edges, token
catalog entries, provider records, runtime config snapshots, and the
`workflow_signature`.

Real execution consumes compiled preflight artifacts and same-process secret
handles. It does not re-read original `{{file:...}}` source paths.

## Manifests

Run and node manifests record status, artifact descriptors, workflow identity,
`workflow_signature`, resumed nodes, and Experimental workspace descriptors
when applicable.

Corrupt or untrusted manifests are treated as unusable history for skip/resume
decisions.

## Logs

Provider logs are captured when
`settings.integrations.artifacts.options.log_cli_output` is `true`. Run-level
events and summaries are written under the run `logs/` directory.

## Downstream Artifact Keys

Workflow prompts can reference upstream artifacts with:

- `{{node.output}}`
- `{{node.findings}}`
- `{{node.output_path}}`
- `{{node.findings_path}}`
- `{{node.output_size}}`
- `{{node.findings_size}}`
- `{{node.output_sha256}}`
- `{{node.findings_sha256}}`
