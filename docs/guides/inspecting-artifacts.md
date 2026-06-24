# Inspecting Artifacts

Crewplane writes run state under `.crewplane/`. The mock quickstart writes the
same artifact structure as provider-backed runs, so inspect these files before
switching to real providers.

## Main Directories

```text
.crewplane/
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
.crewplane/execution-stages/<workflow>-<run-id>/
  logs/
    events.ndjson
    summary.md
  preflight/
    execution-plan.json
    manifest.json
    metadata.json
    render-plans.json
    execution-bundle.json
  manifests/
    run.json
    nodes/
  <node-id>/
    logs/
    review-state/
    workspace-state*.json
    workspace-setup/
    workspace-bundles/
    resume-source.json
  workspace-exports/
```

Exact files depend on node mode, provider count, findings, review loops, and
Experimental workspace use.

## Result Directory

```text
.crewplane/execution-results/<workflow>-<run-id>/
  <node-id>-result.md
  <node-id>-findings.md
  generated-files/<stage>/<task>/...
```

Result filenames use safe, bounded names derived from node IDs. The artifact
reference documents the stable artifact keys available to downstream workflow
nodes.

## Preflight Bundle

A successful real run writes compiled execution-plan artifacts under the run's
`preflight/` directory. Runtime consumes those compiled artifacts instead of
reparsing templates or rereading original file-token source paths. The root
`.crewplane/preflight/fingerprint.key` is a persisted fingerprint key, not the
per-run execution bundle.

## Resume Evidence

Filesystem-backed resume writes evidence into the new run directory, including
hydrated results and per-node `resume-source.json` files when nodes are resumed.
The run manifest records resumed node IDs.

## Experimental Workspace Evidence

Experimental workspace-enabled runs can write:

- workspace state files such as `workspace-state.json` or
  `workspace-state-<slug>.json`
- workspace setup logs and metadata under `workspace-setup/`
- workspace bundles under `workspace-bundles/`
- branch export records

Use the run summary first, then inspect node stage directories for detailed
state.

## Support Bundle Starting Points

For a reproducible support handoff, start with:

- command output from `crewplane validate` or `crewplane run --no-live`
- `.crewplane/config.yml`
- the workflow `.task.md`
- `.crewplane/execution-stages/<run-key>/logs/summary.md`
- `.crewplane/execution-stages/<run-key>/logs/events.ndjson`
- relevant node output and provider log files

See [reproducible support bundle](../safety/reproducible-support-bundle.md) for
redaction guidance.
