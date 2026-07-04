# Artifacts Reference

Crewplane writes project-local state under `.crewplane/`.

For human inspection, start with
[Inspecting Run Records](../guides/inspecting-artifacts.md). Use this reference
when you need exact paths, stable workflow template keys, or skip/resume
metadata.

| Need | Start with |
| --- | --- |
| Human run overview | `execution-stages/<run-key>/logs/summary.md` |
| Event timeline | `execution-stages/<run-key>/logs/events.ndjson` |
| Skip/resume evidence | `execution-stages/<run-key>/manifests/run.json` |
| Final node outputs | `execution-results/<run-key>/` |

![Run-record tree showing `.crewplane/execution-stages/<run-key>` for logs, preflight, manifests, and node artifacts, and `.crewplane/execution-results/<run-key>` for final outputs and findings.](../images/run-record-tree.png)

Stage directories contain run-local logs, preflight bundles, manifests, and node
artifacts. Result directories contain consolidated outputs, findings, and
generated files intended for inspection or downstream handoff.

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

Each non-dry `crewplane run` allocates:

```text
.crewplane/execution-stages/<run-key>/
.crewplane/execution-results/<run-key>/
```

`<run-key>` is the filesystem directory name for one run. It has the shape
`<workflow-slug>--<workflow-hash>-<run-id>`, for example
`single-agent-review--5e34bc54c79a-20260629-202539`.

Stage run contents can include:

```text
logs/events.ndjson
logs/summary.md
preflight/execution-plan.json
preflight/dependency-graph.json
preflight/manifest.json
preflight/metadata.json
preflight/render-plans.json
preflight/execution-bundle.json
preflight/runtime-config-snapshot.json
preflight/static-resources.json
preflight/static-files/*
preflight/summary.md
preflight/token-catalog.json
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

Consolidated result and findings Markdown uses human-readable section headings.
Stable provider task IDs remain in stage artifact filenames, logs, manifests,
and review-loop state.

## Stable Keys And Filenames

Workflow template keys are the stable interface for downstream nodes.
Human-readable result filenames are stable enough to inspect, but code should
prefer workflow template keys or manifest descriptors.

Provider log filenames, task IDs, and review-loop state files are implementation
details for debugging and support.

## Preflight Files

The root `.crewplane/preflight/fingerprint.key` stores the fingerprint key used
for stable secret fingerprints when it can be persisted. Each executed run also
writes a run-local `preflight/` directory. Preflight compiles static resources,
render plans, dependency edges, token catalog entries, provider records, runtime
config snapshots, and the `workflow_signature`.

Runtime execution consumes compiled preflight artifacts and same-process secret
handles. It does not re-read original `{{file:...}}` source paths.

## Manifests

Run and node manifests record status, artifact descriptors, workflow identity,
`workflow_signature`, resumed nodes, and Experimental workspace descriptors
when applicable.

Corrupt or untrusted manifests are treated as unusable history for skip/resume
decisions.

Duplicate skip decisions reuse a previous successful run only when the recorded
`workflow_signature` and required artifacts are usable. Resume decisions hydrate
completed node-boundary artifacts from a failed or cancelled run into a new run.
`crewplane run --force` bypasses both behaviors and records a new run.

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
