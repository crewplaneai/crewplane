# Inspecting Run Records

Crewplane writes each run under `.crewplane/`. Use this guide after
`crewplane run` when you want to read the files it wrote. The quickstart
uses the same run-record structure as provider-backed runs, so the same
inspection steps work before and after you switch to real providers.

## Find The Latest Run

Run these commands from the project directory:

```bash
crewplane runs list
crewplane runs show latest
```

The list uses saved execution manifests and persisted start times, not directory
modification times. It works without current config, authored workflows, or
available providers, and does not repair or modify the saved runs. `latest` means
the newest valid execution record, not a live process. Preflight-only attempts and
invalid manifests are excluded with diagnostics on stderr.

Copy a selector from the Run column to inspect the same run later. For scripts,
use the full key printed by `show`, for example
`single-agent-review--5e34bc54c79a-20260629-202539`. Use that full value anywhere
this guide shows `<run-key>`. See
[Workflow IDs, Run IDs, and Run Keys](running-workflows.md#workflow-ids-run-ids-and-run-keys)
for the breakdown, and the [saved-runs command reference](../reference/commands.md#crewplane-runs)
for selector precedence, ambiguity, and local-time ordering limits.

## Open These First

![Run-record tree showing `.crewplane/execution-stages/<run-key>` for logs, preflight, manifests, and node artifacts, and `.crewplane/execution-results/<run-key>` for final outputs and findings.](../images/run-records/run-record-tree.png)

Start with the files a user usually needs, then go deeper only when you are
debugging.

1. Read the saved summary with `crewplane runs show latest`.
2. Open the summary in its associated application with `crewplane runs open latest`,
   or the existing results directory with `crewplane runs open latest --results`.
3. Read raw events with `crewplane runs logs latest` if event sequence matters.
4. Select a provider log with `crewplane runs logs latest --node NODE`, replacing
   `NODE` with its exact logical ID.
5. Open manifests when debugging duplicate skip, resume, or support issues.

The `show` header reports the manifest outcome separately from the saved Markdown
snapshot, which may predate finalization or recovery. Relative links in terminal
output are paths based at the printed summary directory. Use `show` on headless
systems; `open` only submits a desktop open request. Missing summaries are not
regenerated, and a missing results directory is not created.

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

## Keep Copies Of Changed Files

Keeping extra copies in the run results is optional. For Git-based projects,
you can review changes with Git without asking executors for a file list on
every run.

When you want Crewplane to retain copies of files changed in the project root,
ask the executor to finish its response with an exact `## Generated Files`
heading and a list of paths relative to that root. List only files created or
changed during that invocation. For example, if the executor changed these two
files:

```markdown
## Generated Files

- examples/text-cleanup/normalize_spacing.py
- examples/text-cleanup/test_normalize_spacing.py
```

If no files changed, omit the section. Put explanations in the rest of the
response, rather than beside the listed paths. A later summary should refer to
earlier work without listing those files as its own changes.

Crewplane checks the list against Git changes since that invocation began.
Without a usable Git starting state, it cannot retain these files through
project-root capture. Managed workspaces can also capture changes within their
own working directories. After the run, check `generated-files/` and the run
logs: a successful text result does not guarantee that every requested file copy
was saved. See the [artifact reference](../reference/artifacts.md#results)
for the full capture rules.

## Read The Timeline And Logs

Use `crewplane runs logs latest` for the raw event stream. It is useful for
checking when nodes started, finished, failed, or were skipped. The underlying
file is `.crewplane/execution-stages/<run-key>/logs/events.ndjson`.

To inspect or follow one provider log, replace `NODE` with an exact logical node
ID, including any import namespace or foreach suffix:

```bash
crewplane runs logs latest --node NODE
crewplane runs logs latest --node NODE --follow
```

Selection comes from saved invocation events, not filenames or modification
times. Metadata goes to stderr; stdout stays raw log text. For reused work, the
command identifies the source run instead of presenting old output as fresh work.
See the [selection rules](../reference/commands.md#crewplane-runs)
for timestamp ties, terminal-only references, and checkpoint fallback.

Follow reads the selected file from the beginning and stays on that file until
Ctrl-C, even at EOF after completion. It never switches to newer runs or rounds.
Ctrl-C ends only this reader. A missing file fails immediately; deletion,
replacement, or truncation stops following instead of rotating to another file.

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
node directories, and Workspace state when enabled.
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
Workspace use.

## Preflight Files

A successful non-dry run writes compiled preflight artifacts under:

```text
.crewplane/execution-stages/<run-key>/preflight/
```

Open these files when you need the compiled execution plan, dependency graph,
render plans, static resources, token catalog, or runtime config snapshot. The
root `.crewplane/preflight/fingerprint.key` is a persisted fingerprint key, not
the per-run execution bundle.

## Workspace Evidence

Workspace-enabled runs can write:

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

Or return to the [Guides](../index.md#guided-tutorial-track).
