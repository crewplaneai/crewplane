# Artifacts Reference

Crewplane writes project-local state under `.crewplane/`.

## Root Layout

```text
.crewplane/
  config.yml
  workflows/
  preflight/
    fingerprint.key
  locks/
  execution-stages/
  execution-results/
```

The output directories are hyphenated:

- `.crewplane/execution-stages/`
- `.crewplane/execution-results/`

## Stage Runs

Each real run allocates:

```text
.crewplane/execution-stages/<run-key>/
.crewplane/execution-results/<run-key>/
```

`<run-key>` is a safe generated name derived from workflow name and run ID.

Stage run contents can include:

```text
logs/events.ndjson
logs/summary.md
preflight/execution-plan.json
preflight/manifest.json
preflight/metadata.json
preflight/render-plans.json
preflight/execution-bundle.json
manifests/run.json
manifests/nodes/*.json
<node-id>/logs/<provider>/*.log
<node-id>/review-state/review-loop-status.json
<node-id>/workspace-state*.json
<node-id>/workspace-setup/*.log
<node-id>/workspace-setup/*.json
<node-id>/workspace-bundles/*.bundle
<node-id>/resume-source.json
workspace-exports/*.json
```

Exact files depend on node mode and enabled features. Workspace files are
present only for Experimental workspace isolation runs.

## Results

Consolidated node artifacts are written under the matching result directory:

```text
.crewplane/execution-results/<run-key>/<node-id>-result.md
.crewplane/execution-results/<run-key>/<node-id>-findings.md
.crewplane/execution-results/<run-key>/generated-files/<stage>/<task>/...
```

Node result filenames use safe, bounded names derived from node IDs. Findings
files are present for nodes that declare `findings: true`. Generated-file
artifacts are present when Crewplane detects provider-created files that should
be copied into the result tree.

## Preflight Files

The root `.crewplane/preflight/fingerprint.key` stores the fingerprint key used
for stable secret fingerprints when it can be persisted. Each real run also
writes a run-local `preflight/` directory. Preflight compiles static resources,
render plans, dependency edges, token catalog entries, provider records, runtime
config snapshots, and the `workflow_signature`.

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
