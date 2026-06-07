# ADR 0004: Run Logs and Live Log Streaming

## Status
Accepted

## Date
2026-04-10

## Updated
2026-05-11

## Decision
Adopt two complementary observability layers for workflow execution:

1. Always-on operational run logs written as artifacts under the run root.
2. A progressive disclosure model for the tmux live dashboard right pane.

Operational logs are part of the auditable artifact model. They are generated
for every real workflow run regardless of `--no-live`, TTY state, or tmux
availability.

Live log streaming stays fact-based. The tmux dashboard derives liveness from
the invocation state and local log file metadata that already exist, then lets
users explicitly enter a raw log inspector when they need exact lines or
scrollback. The orchestrator does not infer provider-specific inner phases such
as `thinking`, `tool_running`, or `streaming` in v1.

## Context
The orchestrator runs external AI CLIs programmatically. Those tools can have
long quiet periods during model work, tool use, retries, or internal UI phases.
The orchestrator cannot force upstream CLIs to emit richer live signals, and
the project deliberately avoids understanding provider internals as orchestration
state.

When invoked programmatically, many AI CLIs:

- do not stream the same rich signals they expose in interactive TUI mode
- do not emit stable structured progress events
- can produce long quiet windows while still actively working

The product still needs two forms of observability:

- postmortem visibility for headless and failed runs
- live liveness clarity during interactive runs

The project architecture already makes disk artifacts the blackboard between
providers, so run-level operational logs belong in the artifact tree rather than
in hidden process-local state or a general Python logging rollout.

## Original Industry Notes
The original log-streaming analysis recorded these non-normative observations
about AI CLI output visibility:

| Tool | What it shows | Known gaps |
| --- | --- | --- |
| Claude Code | Spinner plus elapsed time, optional verbose thinking visibility, status line indicators | Users can still see black-box periods during long tool execution |
| Codex CLI | Strong interactive TUI streaming in native mode | Programmatic or batch usage can still feel opaque during long thinking/tool phases |
| Gemini CLI | PTY-based interactive output and response streaming | Structured intermediate event streams are still evolving |
| Kilo | Step/checkpoint-oriented flow with approvals and orchestration modes | Newer ecosystem surface and less mature observability conventions |

Keep this section source-backed before using it as external evidence. The
decision below does not depend on these tool-specific claims; it depends on the
stable local constraint that programmatic CLI execution cannot assume a portable
structured progress stream.

## Operational Run Logs
Run-level operational logs are stored under:

```text
.orchestrator/execution-stages/<workflow>-<run_id>/logs/
```

The run-level files are:

- `events.ndjson`
- `summary.md`

Existing provider CLI logs remain under each node:

```text
.orchestrator/execution-stages/<workflow>-<run_id>/<node>/logs/<provider>/<task>.log
```

Run manifests remain metadata-only under:

```text
.orchestrator/execution-stages/<workflow>-<run_id>/manifests/*.json
```

`logs` and `manifests` are reserved run-root names and are invalid workflow node
IDs. This prevents node output directories from colliding with run-owned
operational paths.

### Runtime Design
Persistent logging is implemented as a built-in observer in `observability/`.
`ObservabilityHub` is the fanout point for:

- persistent run logging through `PersistentRunLogger`
- tmux observers when live UI is active
- in-memory dashboard state updates

`workflow_runner` creates an `ObservabilityHub` for real runs and always passes
`event_sink=hub.emit` into runtime execution. The persistent logger is included
whether or not live UI is active. tmux is an optional observer and degrades
cleanly when disabled, unavailable, or unable to start.

This is intentionally not a general Python `logging` rollout. The feature is
run-scoped, artifact-backed, local-first, and auditable on disk.

### Public Contracts
`ArtifactStorePort` exposes run-level log paths:

- `get_run_log_dir() -> Path`
- `get_orchestrator_event_log_path() -> Path`
- `get_orchestrator_summary_path() -> Path`

Stage finalization returns structured facts:

- `stage_name`
- `result_file`
- `findings_file`
- `included_outputs`
- `skipped_empty_outputs`
- `warnings`

The runtime uses those facts to emit operational events instead of relying on
ad hoc prints from artifact finalization.

`InvocationContext` includes an optional diagnostics callback used by built-in
runtime and invoker code. `InvokerAdapterPort` is not widened; invoker adapters
continue to receive the same invocation entry point and may ignore diagnostics.

### Implementation Map
- `cli/workflow_runner.py` creates and uses `ObservabilityHub` for real runs,
  passes the persistent observer in all cases, adds tmux observers only when live
  UI is available, and records CLI-visible run warnings as `runtime_log` events.
- `observability/runtime.py` fans events and snapshots out to active observers.
- `observability/persistent.py` writes `events.ndjson`, rewrites `summary.md`,
  and renders terminal run summaries from the same persisted event stream.
- `observability/events/__init__.py` defines lifecycle events, `runtime_log`, dashboard
  state transitions, NDJSON serialization, and warning/error recent-event
  updates for node-scoped runtime logs.
- `runtime/execution/common.py` emits lifecycle events, converts invocation
  diagnostics into `runtime_log`, and emits stage-finalization facts.
- `runtime/agent/invoker.py` emits diagnostics for retry scheduling, quota retry
  waits, and stderr fallback without changing invocation success/failure
  semantics.
- `artifacts/result_writer.py` returns `StageFinalizeResult` instead of relying
  on ephemeral warnings.
- `artifacts/directory_manager.py` owns run-level log paths while keeping
  node-local provider log paths unchanged.

## Event Model
The lifecycle event set is:

- `workflow_started`
- `workflow_finished`
- `workflow_failed`
- `node_started`
- `node_finished`
- `node_failed`
- `node_blocked`
- `invocation_started`
- `invocation_finished`
- `invocation_failed`

Operational facts use:

- `runtime_log`

`runtime_log` supports:

- `level`: `debug`, `info`, `warning`, or `error`
- `message`: short human-readable fact
- `operation`: stable short code such as `retry_scheduled`,
  `quota_retry_scheduled`, `stderr_fallback`, or `stage_finalized`
- `attributes`: flat machine-readable values, limited to strings, numbers,
  booleans, or nulls
- optional context fields such as `node_id`, `task_id`, `provider`, `role`,
  `model`, `duration_ms`, `output_file`, and `log_file`

`apply_event()` does not change workflow or node status for `runtime_log`.
Warning and error runtime logs with a node context may be appended to that
node's recent dashboard events.

## What Gets Logged
`events.ndjson` persists lifecycle events and high-signal runtime facts. Runtime
facts include:

- retry scheduled
- quota retry scheduled
- stderr used as final output because stdout was empty
- stage finalization completed
- empty output skipped during result consolidation
- blocked node with unsatisfied dependency details
- observer startup and observer failure warnings when persistence is available
- run-level warnings that are also printed by the CLI, such as tmux unavailable
- prompt budget warnings, review-loop diagnostics, artifact drift diagnostics,
  and related runtime warnings emitted by newer runtime paths

The logging policy excludes sensitive or high-volume content:

- prompt bodies
- model output bodies
- environment variable values
- file contents
- full command lines that may expose secrets

For file references, runtime logs should record paths, existence, sizes, counts,
and clipped error text only.

## File Formats
`events.ndjson` is append-only during the run. Each line is one JSON object with
at least:

- `timestamp`
- `event_type`
- `workflow_name`
- `run_id`

Records include applicable event context and payload fields such as:

- `level`
- `message`
- `operation`
- `node_id`
- `task_id`
- `provider`
- `role`
- `model`
- `audit_round_num`
- `round_num`
- `duration_ms`
- `output_file`
- `log_file`
- `error`
- `attributes`

Invocation terminal events may also include spend-observability fields such as
CLI capture status, output extraction status, provider usage status, provider
token buckets, visible lower-bound token estimates, configured cost estimate,
cost confidence, usage parse errors, and classified failure details.

`summary.md` is rewritten on observer stop. It is concise and anomaly-first.
Current sections include:

- run status, workflow name, run id, start time, completion time, and elapsed time
- spend observability rollups
- per-node outcomes with status, duration, and result file path
- warnings and errors ordered by severity then time
- artifact references for invocation output files and provider logs

When an invocation succeeds with empty stdout and stderr is used as output,
`summary.md` explicitly records that stdout was empty, stderr was used as output,
and the provider log contains the original stderr lines.

## Live Dashboard Progressive Disclosure
The tmux right pane defaults to a compact, fact-based summary/tail rather than
raw log passthrough. It uses the following local signals:

| Signal | Source | Use |
| --- | --- | --- |
| Invocation status | `InvocationRuntimeState.status` | left panel and right invocation header |
| Provider name | `InvocationRuntimeState.provider` | right invocation header |
| Role | `InvocationRuntimeState.role` | right invocation header |
| Audit/round number | `InvocationRuntimeState.audit_round_num` and `round_num` | right invocation header |
| Log file path | `InvocationRuntimeState.log_file` | selected invocation tail and inspect mode |
| Invocation start time | `InvocationRuntimeState.started_at` | elapsed running time |
| Log file size | `Path.stat().st_size` | liveness metadata |
| Log modification time | `Path.stat().st_mtime` | quiet/stale metadata |
| Body content past the provider log header | `_read_log_tail(...)` | compact tail and first-output detection |

Dashboard-mode right-pane states are:

### Pending

```text
Node Output: backend.auth
Status: pending

Waiting for dependencies to complete...
```

### Running, No Tail Content Yet

```text
Node Output: backend.auth
Status: running

codex/executor/task_001 (round1) [running]
Running for 47s
Log file: 2.3 KB (updated 1s ago)

Awaiting first output from provider...
```

### Running, Tail Content Available

```text
Node Output: backend.auth
Status: running

codex/executor/task_001 (round1) [running]

[... tailed log output lines ...]
```

### Running, Tail Quiet

```text
Node Output: backend.auth
Status: running

codex/executor/task_001 (round1) [running]
Running for 3m12s
Log file: 2.3 KB (updated 1m25s ago)

No new output for 1m25s.
Provider still running; waiting for new output.
```

If `started_at` is missing or invalid, elapsed-time rendering is omitted.

### Tmux Options
The tmux UI supports:

- `quiet_after_seconds`: when a running invocation has not appended new log
  output for this long, render quiet-state liveness messaging
- `log_tail_lines`: optional fixed cap for tailed compact-dashboard lines;
  omission or `null` lets the renderer fit the right pane height dynamically

Tier 1 was rendering-only: `_render_selected_output()` gained elapsed time from
`invocation.started_at`, log size and update age from `Path.stat()`, explicit
quiet/growing messaging, and graceful omission when `started_at` is missing or
invalid. No core orchestration changes were required.

Tier 2 added the observer-local liveness options above. The original analysis
also mentioned a metadata verbosity option; that option is not implemented and
is tracked in the deferred-work note.

`monitor-silence` is not used. The pane process redraws continuously with a
`clear` plus `cat` loop, so tmux pane silence is not a reliable proxy for
provider silence in this architecture.

When tmux UI is active, `ObservabilityHub` runs with `refresh_per_second=0`.
Elapsed-time rendering is computed in the tmux runtime refresh thread from local
monotonic time plus `invocation.started_at`, not from continuously ticking
snapshots.

### On-Demand Raw Log Inspect Mode
The compact summary remains the default. Pressing `Enter` swaps the right pane
onto the selected invocation's real log with:

```text
tail -n +1 -F <log_path>
```

Inspect mode stays locked to the chosen log even if dashboard selection changes.
`Esc` restores the compact dashboard view. tmux handles scrolling, copy-mode,
and scrollback against the real file instead of the dashboard renderer simulating
those behaviors.

### Approach Comparison
| Approach | Solves "is it dead?" | Clean output | Architecture fit |
| --- | --- | --- | --- |
| Raw log passthrough | Partially | No, provider output is often noisy and metadata-heavy | Useful as explicit inspect mode, poor default |
| Elapsed timer only | Yes | Yes | Good but incomplete |
| Progressive disclosure | Yes | Yes | Uses existing snapshots and file metadata |

## Rationale
### Always-On Run Logs
`logs/` at the run root is the right boundary because the run root is already
owned by the orchestrator. A nested `logs/orchestrator/` directory would add
indirection without solving a current collision.

`manifests/` stays metadata-only so deterministic workflow-signature metadata
and execution metadata are not mixed with operational diagnostics.

Always-on logging matches the project's auditable-on-disk model and avoids blind
spots in headless runs.

### Progressive Disclosure
Fact-based rendering uses what the runner knows locally: invocation lifecycle,
elapsed time, file size, file modification time, and log tail contents. This
avoids interpreting provider-specific raw output into a premature standard
schema.

Keeping liveness derivation inside the UI adapter preserves core DAG semantics,
workflow parsing, and provider invocation contracts.

Raw logs are still available, but only as an explicit inspect mode. This keeps
the default UI readable while preserving exact log inspection when users need it.

## Architecture Alignment
- The observer contract remains stable for implemented behavior:
  `on_snapshot(event, snapshot)`.
- Rendering stays inside the UI adapter boundary.
- Core DAG semantics and workflow parsing are untouched.
- Replaceable UI, invoker, and artifact boundaries remain respected.
- Future structured progress hints should be a versioned extension surface under
  the ports contract.

## Consequences
### Positive
- Headless and `--no-live` runs produce durable operational evidence.
- Warnings can be recorded without changing workflow success semantics.
- Users get explicit live liveness context such as running time, log size, and
  quiet duration.
- Users can inspect full provider logs with tmux-native scrollback and copy-mode.
- The design preserves CLI-first provider integration and artifact-first auditability.

### Negative
- The live dashboard does not expose semantic progress percentages or inner
  provider activity phases.
- Provider-specific progress hints need a future extension contract before they
  can be safely standardized.
- The tmux liveness heuristics are not yet factored into a reusable helper for a
  future web UI.
- Runtime snapshots are copied for observer delivery, so high-frequency progress
  events would need rate limiting or coalescing before being added.

## Defaults and Policy
- Orchestrator run logs are always generated for real runs.
- There is no `log_orchestrator_output` flag in v1.
- `settings.log_level` does not control persistence; all lifecycle events are
  persisted. If log-level settings are wired later, they may control only extra
  `runtime_log` verbosity.
- A run may have a `succeeded` manifest while warnings are recorded in
  `events.ndjson` and `summary.md`.
- Empty-output and stderr-fallback cases are warnings, not automatic failures in
  v1.
- tmux live mode depends on provider CLI output log capture being enabled.

## Rejected Alternatives
1. Raw log passthrough as the default view. It partially answers "is it dead?"
   but is noisy and less readable than a compact state/tail view. Raw logs remain
   available through inspect mode.
2. tmux `monitor-silence`. The dashboard panes redraw constantly, so pane silence
   is not provider silence.
3. Structured progress hints in v1. Provider subphase semantics are not portable
   yet, and high-frequency updates would require explicit transport, persistence,
   and rate-limiting design.
4. A general Python logging rollout. This would not match the run-scoped
   artifact model and would blur operational diagnostics with process logging.
5. Widening `InvokerAdapterPort` for diagnostics. `InvocationContext` can carry
   optional diagnostics without changing the adapter contract.

## Validation Coverage
The implementation is covered by deterministic local tests for:

- run-root `logs/events.ndjson` and `logs/summary.md`
- persistent logger summary rendering
- coexistence of persistent and tmux observers
- `event_sink` attachment on headless and `--no-live` runs
- warning recording when live UI is unavailable
- retry, quota retry, and stderr-fallback diagnostics
- nonzero provider CLI exits still producing `invocation_failed`
- stage finalization facts and empty-output warnings
- node-local provider logs remaining unchanged
- reserved `logs` and `manifests` node IDs
- tmux compact rendering, auto-sized tails, quiet-state messaging, inspect-mode
  key bindings, locked inspection logs, and resize recovery

## Follow-Ups
None currently.
