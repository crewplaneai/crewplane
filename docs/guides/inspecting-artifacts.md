# Inspecting Artifacts

Crewplane writes run state under `.orchestrator/`.

## Main Directories

```text
.orchestrator/
  config.yml
  workflows/
  preflight/
  locks/
  execution-stages/
  execution-results/
```

`execution-stages` contains run-local state, logs, manifests, preflight bundles,
node directories, and Experimental workspace state when enabled.
`execution-results` contains consolidated node outputs and findings.

## Stage Run Directory

```text
.orchestrator/execution-stages/<workflow>-<run-id>/
  logs/
    events.ndjson
    summary.md
  manifests/
    run.json
    nodes/
  <node-id>/
    logs/
    review-state/
    workspace-state.json
    resume-source.json
```

Exact files depend on node mode, provider count, findings, review loops, and
Experimental workspace use.

## Result Directory

```text
.orchestrator/execution-results/<workflow>-<run-id>/
  <node-id>-result.md
  <node-id>-findings.md
```

The artifact reference documents the stable artifact keys available to
downstream workflow nodes.

## Preflight Bundle

A successful real run writes compiled execution-plan artifacts. Runtime consumes
those compiled artifacts instead of reparsing templates or rereading original
file-token source paths.

## Resume Evidence

Filesystem-backed resume writes evidence into the new run directory, including
hydrated results and per-node `resume-source.json` files when nodes are resumed.
The run manifest records resumed node IDs.

## Experimental Workspace Evidence

Experimental workspace-enabled runs can write:

- workspace state files
- workspace setup logs and metadata
- workspace bundles
- branch export records

Use the run summary first, then inspect node stage directories for detailed
state.
