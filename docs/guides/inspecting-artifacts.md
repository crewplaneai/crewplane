# Inspecting Run Records

Crewplane writes each run under `.crewplane/`. Use this guide after
`crewplane run` when you want to read the files it wrote. The quickstart
uses the same run-record structure as provider-backed runs, so the same
inspection steps work before and after you switch to real providers.

## Find The Latest Run

If the terminal output scrolled away, find the latest stage run directory:

```bash
ls -1td .crewplane/execution-stages/*/ | head -n 1
```

Example output:

```text
.crewplane/execution-stages/single-agent-review--5e34bc54c79a-20260629-202539/
```

The directory name is the run key. In this example, the run key is
`single-agent-review--5e34bc54c79a-20260629-202539`. Use the full value
anywhere these docs show `<run-key>`. See
[Run Keys And Run IDs](running-workflows.md#run-keys-and-run-ids) for the full
breakdown.

## Open These First

![Run-record tree showing `.crewplane/execution-stages/<run-key>` for logs, preflight, manifests, and node artifacts, and `.crewplane/execution-results/<run-key>` for final outputs and findings.](../images/run-record-tree.png)

Start with the files a user usually needs, then go deeper only when you are
debugging.

1. Open `logs/summary.md`.
2. Open the final result file under `.crewplane/execution-results/<run-key>/`.
3. Open `logs/events.ndjson` if event sequence matters.
4. Open node logs only when provider output matters.
5. Open manifests when debugging duplicate skip, resume, or support issues.

| Need | Open |
| --- | --- |
| Human-readable run overview | `.crewplane/execution-stages/<run-key>/logs/summary.md` |
| Final node outputs and findings | `.crewplane/execution-results/<run-key>/` |
| Event timeline | `.crewplane/execution-stages/<run-key>/logs/events.ndjson` |
| Provider output | Node log files under `.crewplane/execution-stages/<run-key>/<node-id>/logs/` |
| Skip or resume evidence | `.crewplane/execution-stages/<run-key>/manifests/run.json` |

## Find Final Outputs

```text
.crewplane/execution-results/<run-key>/
  <node-id>-result.md
  <node-id>-findings.md
  generated-files/<stage>/<task>/...
```

Result filenames use safe, bounded names derived from node IDs. The result
directory is usually the best place to start when you only need the workflow's
final output.

Use:

- `<node-id>-result.md` for a node's consolidated output.
- `<node-id>-findings.md` when the node produced findings.
- `generated-files/` when a provider wrote generated files through Crewplane.

Result section headings are human-readable. Inspect stage artifacts, logs,
manifests, or review-loop state when you need stable provider task IDs.

## Read The Timeline And Logs

Use `.crewplane/execution-stages/<run-key>/logs/events.ndjson` when you need the
ordered event stream. It is useful for checking when nodes started, finished,
failed, or were skipped.

Provider logs live under each node stage directory:

```text
.crewplane/execution-stages/<run-key>/
  logs/
    events.ndjson
    summary.md
  <node-id>/
    logs/
```

Exact log filenames depend on node mode, provider count, and retry behavior. Use
the run summary first, then open node logs only when you need provider output.

## Check Skip Or Resume Evidence

Use `.crewplane/execution-stages/<run-key>/manifests/run.json` when you need to
understand whether a run executed normally, reused prior results, or resumed
from a previous failed or cancelled run.

Common evidence:

- `workflow_signature` identifies the compiled context used for duplicate skip
  and resume decisions.
- `resumed_nodes` records nodes hydrated from a prior run.
- `resume_source_run_id` and `resume_source_run_key_name` point at the prior run
  when resume hydration happened.
- `<node-id>/resume-source.json` appears inside resumed node stage directories.

For the behavior behind those fields, see
[Duplicate Skip](running-workflows.md#duplicate-skip) and
[Resume](running-workflows.md#resume).

## Full Directory Reference

Use the full tree when you need exact artifact names for debugging, resume
evidence, support bundles, or downstream workflow references.

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
.crewplane/execution-stages/<run-key>/
  logs/
    events.ndjson
    summary.md
  preflight/
    dependency-graph.json
    execution-plan.json
    manifest.json
    metadata.json
    render-plans.json
    execution-bundle.json
    runtime-config-snapshot.json
    static-resources.json
    static-files/
    summary.md
    token-catalog.json
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

## Preflight Files

A successful non-dry run writes compiled preflight artifacts under:

```text
.crewplane/execution-stages/<run-key>/preflight/
```

Open these files when you need the compiled execution plan, dependency graph,
render plans, static resources, token catalog, or runtime config snapshot. The
root `.crewplane/preflight/fingerprint.key` is a persisted fingerprint key, not
the per-run execution bundle.

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

- command output from `crewplane validate` or the `crewplane run` command used
- `.crewplane/config.yml`
- the workflow `.task.md`
- `.crewplane/execution-stages/<run-key>/logs/summary.md`
- `.crewplane/execution-stages/<run-key>/logs/events.ndjson`
- relevant node output and provider log files

See [reproducible support bundle](reproducible-support-bundle.md) for
redaction guidance.

## Next

Continue to [Workflow Authoring](workflow-authoring.md) to write your own
Markdown workflow after you understand how runs are executed and recorded.

Or return to the [Guides](../index.md#guides).
