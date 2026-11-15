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

![Run-record tree showing `.crewplane/execution-stages/<run-key>` for logs, preflight, manifests, and node artifacts, and `.crewplane/execution-results/<run-key>` for final outputs and findings.](../images/run-records/run-record-tree.png)

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
  memory/                 # only after explicit memory init
    store.json
    store.lock
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
`<workflow-id>-<run-id>`, for example
`single-agent-review--5e34bc54c79a-20260629-202539`.

Stage run contents can include:

```text
logs/events.ndjson
logs/summary.md
preflight/execution-plan.json
preflight/memory-selection.json
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
manifests/completion.json
manifests/memory/evidence.json
manifests/memory/extraction.lock
manifests/memory/extraction.json
manifests/memory/candidates.json
manifests/nodes/*.json
manifests/review-checkpoints/*.json
<node-id>/logs/<provider>/*.log
<node-id>/review-state/review-loop-status.json
<node-id>/review-state/checkpoints/workspaces/*.json
<node-id>/workspace-state*.json
<node-id>/workspace-setup/*.log
<node-id>/workspace-setup/*.json
<node-id>/workspace-bundles/*.bundle
<node-id>/resume-source.json
workspace-exports/*.json
```

Exact files depend on node mode and enabled features. Workspace files are
present only for Workspace isolation runs.

For review-loop nodes, `review-loop-status.json` records the executor and
reviewer outputs selected when the loop ends. Each entry identifies the
provider, task, role, audit round, local round, relative path, file size, and
SHA-256 digest. Crewplane verifies both the status details and the referenced
files before building the final node artifacts. If verification fails, the node
fails.

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

Generated-file ownership depends on the workspace boundary:

- In an isolated managed workspace, Crewplane owns the workspace for the
  invocation and may capture filesystem changes.
- In the shared project root, Crewplane captures only files explicitly listed
  by the provider under a `## Generated Files` section and verified by Git as
  changed since the invocation began. Without a usable Git baseline,
  generated-file capture fails closed.

Snapshot capture limits and snapshot copy failures do not invalidate an
otherwise successful provider result. Crewplane preserves the result, captures
accepted files, emits an `artifact_capture_partial` or
`artifact_capture_failed` runtime-log event, and records rejected-file metadata
in the generated-file snapshot metadata. Rejection metadata keeps the exact
`rejected_file_count` while bounding detailed `rejected_files` records to 100;
`rejected_files_truncated` reports whether details were omitted. If later
publication into the consolidated result tree fails, Crewplane omits the failed
generated-file link, preserves the consolidated result, and emits a
`stage_finalize_warning` event.

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

## Project Memory Records

`.crewplane/memory/store.json` contains records with immutable lesson revisions,
retained evidence and proposal identities, dispositions, acceptance metadata, and
one current accepted pointer per record. `store.lock` is a persistent advisory
lock; normal release never unlinks or replaces it. Only `memory init` creates
these files. The mutable memory directory is excluded from source capture and
file-token access, including aliases and allowlisted attempts.

The store uses the shared schema version. Its actual serialized size is limited
to 16 MiB; each record including retained revisions is limited to 32 KiB. Invalid
or oversized state is rejected, not trimmed. See
[Project memory](../guides/project-memory.md#retry-extraction-and-handle-failures)
for uncertain publication and initialization handling.

For enabled workflows, `preflight/memory-selection.json` records query
contributions/exclusions, effective limits, policy revisions, selected
record/revision/content/evidence identities, exact rendered context, rendered
size, and omission explanations. It is present even for no-match/no-fit results;
disabled workflows do not write it. The execution plan contains the corresponding
typed selections, including frozen child templates. Plan/receipt disagreement
blocks execution or reuse rather than consulting the current store for repair.

For executed memory-enabled workflows, `manifests/completion.json` records
completion facts when available, including in recall-only mode. This execution
receipt binds the run/plan identity and outcome
to confirmed manifest commit, cleanup, release by that execution-lock handle,
and result display. Explicit extraction requires matching, safe completion
evidence; a terminal manifest or an absent lock alone is insufficient. Missing,
invalid, or unconfirmed completion evidence fails closed. This is not a learning
receipt and does not change the parent terminal manifest. Failure to retain it
skips automatic learning without changing the parent outcome.

Post-run learning writes auxiliary files under the completed parent's
`manifests/memory/`, separate from its immutable terminal outcome:

- `evidence.json`: the bounded host packet, retained source/excerpt identities,
  outcome limitations, provenance, and omissions; serialized size at most 64 KiB.
- `extraction.json`: separate provider-execution, candidate-validation, and import
  status, packet identity, and extraction-run reference.
- `extraction.lock`: persistent per-parent attempt lock. Only its active owner
  may publish extraction status; concurrent extraction commands are rejected
  before reading or replacing another attempt's cleanup proof.
- `candidates.json`: validated proposals and imported candidate identities.

The extraction run has its own normal plan, manifests, results, and usage records.
A node named `memory` still uses its ordinary stage/node-manifest paths; it does
not collide with this reserved auxiliary directory. These records can contain
sensitive task/output content. Forgetting a store record does not erase them.

## Manifests

Run and node manifests record status, artifact descriptors, workflow identity,
`workflow_signature`, resumed nodes, the source of reused review checkpoints,
and Workspace descriptors when applicable.

Terminal fields in `manifests/run.json` depend on the run status. Failed and
cancelled runs include a nonblank reason, while successful runs include neither
a failure nor cancellation reason. For resumed runs, the manifest records the
source run together with the work restored from it. `resumed_nodes` lists only
successful nodes. `resumed_review_checkpoints` separately records each node's
`source_audit`, `source_local_round`, `source_phase`, and immediate `resume_origin`.
A node appears in only one collection. Source-run fields are present when either
collection has entries. Crewplane adds entries after restoring their files.

At the end of a run, Crewplane finishes required post-run work and updates the
event log and summary before recording the terminal status in
`manifests/run.json`. It releases the run lock only after writing the manifest.
A terminal manifest therefore indicates that normal finalization completed.

If Crewplane stops before finalization completes, a later run with the same
workflow identity and signature can recover the stale lock. Recovery preserves
a recorded terminal outcome when it can verify that outcome. Otherwise, it
marks the interrupted run as cancelled with `stale_lock_recovered`. The event
log and summary may be incomplete or show the last state written before the
interruption.

Corrupt or untrusted manifests are treated as unusable history for skip/resume
decisions.

Duplicate skip decisions reuse a previous successful run only when the recorded
`workflow_signature`, node states, results, findings, retained generated files,
sizes, digests, containment, and dependency closure are usable. Crewplane
searches older matching successes if the newest success is incomplete or
corrupt. Resume uses the same checks before copying successful node results and
review checkpoint files from one failed or cancelled run into a new run.
`crewplane run --force` bypasses both behaviors and records a new run.

Atomic artifact publication propagates file-data, replacement, and supported
directory-sync failures. Crewplane suppresses only platform errors that
explicitly mean directory synchronization is unsupported, so an integrity
failure is not reported as a successful publication.

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

## Foreach Expansions

Each `foreach` group records its collection and expansion under:

```text
.crewplane/execution-stages/<run-key>/preflight/expansions/<safe-group>/
  items.json
  items/0.json
  items/1.json
  expansion.json
```

`items.json` contains the collection; each numbered file contains one item.
`expansion.json` marks a completed expansion. A partial bundle cannot authorize
child execution.

A successful group writes raw JSON at its compiled result locator, normally
`.crewplane/execution-results/<run-key>/<group>-result.md`. Each array entry has
`index`, the original `item`, and terminal `results` in child order. Each result
has `local_node_id`, concrete `node_id`, and `output` plus optional `findings`
descriptors. A descriptor contains `path` relative to the group result file's directory,
`size_bytes`, and `sha256`. An empty collection produces exactly `[]`. The group
index is an output artifact, never a findings artifact. Normal downstream prompt
budgets still apply.

Skip, resume, summaries, workspace export, and cleanup use the recorded expansion.
Invalid expansion evidence can prevent reuse or cause cleanup to refuse removal.

Resume copies eligible bundles into a fresh run before successes and selected
review checkpoints. Reuse requires valid original group prerequisites and
matching collection/shared caller artifacts. A prerequisite rerun invalidates
the group's expansion, successes, and open checkpoints. Corrupt child-only
results invalidate their dependency closure while eligible siblings remain
reusable. Missing group output can be rejoined from completed children. Skipping
a successful run requires the entire effective graph, including empty-group
completion, to remain valid.

Generated checkpoints retain normal phase recovery and evidence checks. If a
selected checkpoint cannot be restored, the fresh run fails before provider
calls. `--force` bypasses expansion, success, and checkpoint reuse and
regenerates the collection and all children.
