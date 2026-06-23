# UI Compact Dashboard

> Compact tmux dashboard for single-workflow DAGs now; import grouping later.

## Problem Summary

| Pain Point | Root Cause |
|---|---|
| Not visually pleasing | Plain ASCII, no color, no status indicators |
| Not enough visibility with many nodes | One-pane-per-node grid shrinks panes to unusable sizes |
| Imported workflows are not grouped yet | No hierarchy concept in current DAG rendering |
| Need better local peek into CLI activity | No node selection plus no scrollable per-node log inspection |

Root cause: **mapping every DAG node to a physical tmux pane does not scale**.

---

## Design: Compact Dashboard with Inline DAG Graph

Two-pane tmux layout: **DAG summary** (left) + **selected node output** (right).

The DAG summary uses an inline graph renderer (inspired by `git log --graph`) that shows topology directly in the node list. Nodes are displayed in topological order with box-drawing connectors.

### Full Dashboard Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ Design Review Example | run=a3f8 | ✅1 ⏳1 ⏸1 ⛔1 | 15s             │
├──────────────────────────────┬───────────────────────────────────────┤
│ DAG Summary                  │ Node Output: design.iteration        │
│                              │                                      │
│ ● design.discovery   ✅ 12s  │ -- Round 1/3: codex (executor) --    │
│ ├───┐                codex   │                                      │
│ ▸● │ design.iteration ⏳ 3s  │ $ codex exec --model gpt-5.3-co...   │
│   ├───┘              round1  │ Reading discovery output...          │
│                      codex ->│ Iterating on option B...             │
│                      gemini  │                                      │
│ ● design.decision    ⛔       │ > Drafting implementation plan...     │
│                      blocked  │                                      │
├──────────────────────────────┴───────────────────────────────────────┤
│ [↑/↓] select  [Enter] inspect log  [q] quit run                     │
└──────────────────────────────────────────────────────────────────────┘
```

- `▸` marks the currently selected node.
- Auto-selects the first running node; keyboard controls move selection.
- Completed nodes show elapsed time; failed nodes show error snippet.
- Right pane shows a compact summary/tail for the selected node by default.
- `Enter` swaps the right pane into the selected node's real invocation log so tmux scrolling and copy-mode work against the full file.
- Once opened, inspect mode stays locked to that log until `Escape` restores the compact dashboard.

### DAG Rendering Examples

**Fully parallel (no dependencies):**
```
● backend.auth         ⏳  3s  codex
● backend.billing      ⏳  5s  claude
● backend.payments     ✅ 12s  gemini
● frontend.ui          ⏳  2s  codex
```

**Linear chain:**
```
● design.discovery     ✅ 12s  claude
│
● design.iteration     ⏳  3s  codex -> gemini
│
● design.decision      ⏸       claude
```

**Fan-out -> chain -> fan-in:**
```
● implement.plan       ✅ 12s  claude
├───┐
● │ implement.build    ⏳  3s  codex, gemini
│ │
● │ implement.review   ⏸       codex -> claude
│ │
● │ implement.fixes    ⏸       codex
├───┘
● implement.handoff    ⏸       claude
```

**Failure propagation (blocked vs failed):**
```
● compile.api          ❌  9s  codex
│
● deploy.api           ⛔       blocked
```

---

## Node Status Semantics

`failed` and `blocked` are different and both are required:

- `❌ failed`: node started execution and failed.
- `⛔ blocked`: node never started because at least one dependency did not succeed.

Without `blocked`, downstream causality is hidden in non-trivial DAG failures.

---

## Graph Column Overflow Rule

Graph width scales with concurrent open branches (fan-out), not node count.

| Fan-out | Graph width |
|---|---|
| 0 | 2 chars |
| 1-2 | 4-6 chars |
| 3-5 | 8-12 chars |
| 6+ | capped at 12 |

Cap at 12 chars. Beyond that, show first N branches and collapse the rest:
```
● root              ✅  5s  codex
├─┬─┬─┬─┬─┐
● │ │ │ │ │ child.a ⏳  3s  codex
│ ● │ │ │ │ child.b ⏳  2s  gemini
│ │ ● │ │ │ child.c ⏳  4s  claude
│ │ │ ● │ │ child.d ⏳  1s  codex
│ │ │ │ ● │ child.e ✅  8s  claude
│ │ │ │ │ … child.f ⏳  5s  codex
├─┴─┴─┴─┴─┘
● merge             ⏸       claude
... +3 more
```

---

## DAG Rendering Edge Cases

Additional examples covering topology and status combinations not shown above.

**Single node (degenerate DAG):**
```
● backend.deploy       ⏳  4s  codex
```

**Diamond (fan-out → fan-in, no intermediate chain):**
```
● data.extract         ✅  8s  codex
├───┐
● │ data.transform     ✅ 14s  gemini
│ ● data.validate      ✅ 11s  claude
├───┘
● data.load            ⏳  2s  claude
```

**Fan-in from independent roots (no common ancestor):**
```
● service.auth         ✅  5s  codex
│ ● service.billing    ✅  7s  claude
│ │ ● service.cache    ✅  3s  codex
├─┴─┘
● gateway.compose      ⏳  2s  gemini
```

**Parallel branches with a sequential chain alongside independent parallel nodes:**
```
● plan                 ✅  4s  claude
├───┐
● │ │ impl.api         ⏳  6s  codex
│ ● │ impl.frontend    ⏳  3s  gemini
│ │ ● impl.docs        ⏳  1s  claude
● │ │ impl.api-test    ⏸       codex
├─┴─┘
● release              ⏸       codex
```

**Asymmetric fan-out with mixed sequential and parallel branches:**
```
● design               ✅  5s  claude
├───┐
● │ build.backend      ⏳  8s  codex
│ ● build.frontend     ⏳  3s  gemini, claude
● │ test.backend       ⏸       codex -> gemini
│ │
● │ deploy.backend     ⏸       codex
├───┘
● integration          ⏸       claude
```

**Multiple independent subgraphs:**
```
● backend.api          ✅ 10s  codex
│
● backend.deploy       ⏳  3s  codex
● frontend.build       ✅  6s  gemini
│
● frontend.deploy      ⏸       gemini
```

**Nested fan-out (multi-level branching):**
```
● plan                 ✅  5s  claude
├───┐
● │ impl.frontend      ⏳  8s  codex
│ ● impl.backend       ⏳  6s  gemini
● │ impl.tests         ⏳  3s  codex
├───┘
● review               ⏸       claude
```

**Cascading failure (transitive blocking):**
```
● infra.provision      ❌ 11s  codex
│
● app.deploy           ⛔       blocked
│
● smoke.test           ⛔       blocked
```

**Partial failure in fan-out:**
```
● build.plan           ✅  4s  claude
├───┐
│   ● build.api        ✅  9s  codex
│   ● build.worker     ❌ 15s  gemini
├───┘
● build.integrate      ⛔       blocked
```

**All five statuses in one DAG:**
```
● setup.infra          ✅  6s  codex
├───┐
│   ● deploy.api       ❌ 12s  codex
│   ● deploy.web       ✅  8s  gemini
│   ● deploy.worker    ⏳  5s  claude
├───┘
● verify.e2e           ⛔       blocked
● monitor.dashboard    ⏸       codex
```

---

## Node Selection Mechanics

Keyboard-only node selection:

- tmux owns stdin and installs dashboard bindings in `root`, `copy-mode`, and `copy-mode-vi`.
- `↑/↓` returns focus to the left pane and moves selection.
- `Enter` switches the right pane into the selected node's raw log inspector.
- `Escape` returns from inspect mode to the compact dashboard.
- `q` cancels the running workflow, closes the dashboard, and returns control to the terminal.

Mouse support remains pane-level only (`tmux mouse on`):
- In dashboard mode, click-to-focus stays enabled, but wheel and drag actions are rebound away from tmux copy-mode so the live compact renderer keeps updating.
- In inspect mode, the session returns to tmux's normal key table so native copy-mode, `PageUp`, and wheel scrolling work against the real log pane.

---

## Log Path Refactor (Node-Local, Per Invocation)

The compact dashboard **keeps logs per invocation** (for correctness and parallel safety), but relocates them under each node directory for better discoverability in compact mode.

### Current

```
execution_stages/<workflow>_<run_id>/logs/<provider>/<stage>_<task_id>_round<r>_<run_id>.log
```

### Node-local layout

```
execution-stages/<run_key>/<node_id>/logs/<provider>/<task_id>-round<r>.log
```

### Example

```
execution-stages/
  design-review-20260426-101530/
    design.discovery/
      logs/
        codex/
          codex-executor-0-round1.log
    design.iteration/
      logs/
        codex/
          codex-executor-0-round1.log
        gemini/
          gemini-reviewer-0-round1.log
```

### Notes

- No log multiplexing into a single node file.
- Each invocation still has its own file.
- Compact right pane selects a node, then shows a summary/tail of that node's active/latest invocation log.
- The same pane can be swapped into a raw log inspector on demand without changing layout.

---

## Imported Workflow Grouping (Deferred, Non-Blocking)

Import grouping is explicitly **deferred** and does **not** block compact dashboard delivery.

Current minimal behavior:
- UI receives observer-only topology derived from the compiled preflight plan.
- Imported nodes render as ordinary namespaced nodes (for example `fix.implement.summary`).
- Standalone `mode: input` roots render like any other node and show `input` as their provider label.
- Bound imported input nodes are pruned during composition, so they do not appear as extra placeholder rows in the dashboard.

When grouped import rendering is added later, it should layer on top of this composed-DAG behavior rather than changing runtime semantics.

---

## Rendering Test Strategy

The examples above are the source of truth for graph shape. Tests should
preserve those examples unless this document is intentionally revised.

The UI layer is observer-only. Runtime execution owns workflow semantics, and
the UI renders topology and status from observability snapshots:

1. Workflow loading, composition, validation, and preflight produce the runtime
   workflow topology.
2. `ObservabilityHub` receives runtime events and snapshots the
   `DashboardSnapshot` state and computed topology layout.
3. `render_dag_summary()` renders the left-pane DAG summary from that snapshot.
4. The tmux compact renderer combines the DAG summary with the selected-node
   output pane.

The renderer must not infer execution semantics from display text. DAG shape is
driven by node IDs, dependency edges, node mode, provider metadata, runtime
events, and layout data.

### Fixture Layout

Exact rendering expectations live under
[tests/unit/observability/fixtures/](../../tests/unit/observability/fixtures/).

Use these fixture groups:

- `dag_render/<case-id>/expected.txt` for the inline DAG summary produced by
  `render_dag_summary()`.
- `compact_render/<case-id>/expected-left.txt` for the compact dashboard left
  pane.
- `compact_render/<case-id>/expected-right.txt` for the compact dashboard right
  pane.

Fixture case IDs should describe the rendering behavior, such as
`interleaved_independent_root` or `transitive_shortcut_fanin_chain`. Avoid names
that leak a runtime manifest, local run key, branch name, or one-off user task.

Fixtures are plain text golden files. The helper in
[tests/helpers/render_fixtures.py](../../tests/helpers/render_fixtures.py)
reads them and trims one trailing newline, so assertions compare the exact
visible render output.

### Unit Coverage

Unit tests own exact layout regression coverage:

- [tests/unit/observability/dag_render/](../../tests/unit/observability/dag_render/)
  builds small `WorkflowPlan` graphs and compares the exact DAG summary to
  `dag_render` fixtures.
- [tests/unit/observability/test_dag_render_golden.py](../../tests/unit/observability/test_dag_render_golden.py)
  keeps a direct golden-path render check.
- [tests/unit/observability/test_tmux_rendering.py](../../tests/unit/observability/test_tmux_rendering.py)
  covers compact dashboard panes, selected output, log-tail behavior, quiet
  state, and provider presentation notices with `compact_render` fixtures.

When fixing a node-line, connector-line, lane, fan-in, fan-out, selected-node,
or compact-pane bug, add the smallest unit case that reproduces it and update
or add the corresponding fixture.

### Integration Coverage

Integration tests under
[tests/integration/observability/render/](../../tests/integration/observability/render/)
exercise rendering through the runtime observability path. They build workflows,
run them with the deterministic mock invoker, record snapshots through
`ObservabilityHub`, and render the selected snapshot for fragment-level checks.

Use integration cases when rendering behavior depends on runtime execution,
composition, imported input pruning, failure propagation, persisted event logs,
or run summaries. Keep exact graph geometry in unit fixtures; integration tests
should prove the runtime path selects the right snapshot and surfaces the
expected status, node, and provider fragments.

### Change Workflow

For a UI rendering fix:

1. Reproduce the issue with a focused unit graph or compact-render case.
2. Check the expected shape against this document.
3. Add the expected render shape for the failing case as a fixture under
   [tests/unit/observability/fixtures/](../../tests/unit/observability/fixtures/),
   or update the existing fixture for that case.
4. Add an integration render case only when runtime, composition, status, or log
   behavior is part of the bug.
5. Run the focused render suite before broad validation:

```bash
uv run --extra dev python -m pytest -q \
  tests/unit/observability/dag_render \
  tests/unit/observability/test_dag_render_golden.py \
  tests/unit/observability/test_tmux_rendering.py \
  tests/integration/observability/render
```

Before opening a PR, confirm the fixture name is behavior-oriented and the PR
checklist notes whether a layout fixture was needed.

---

## Implementation Scope

Compact dashboard is the default tmux UI mode. Backward compatibility with grid mode is out of scope.

```
UIAdapterPort.create_runtime()
  └── TmuxCompactRuntime (default)
```

### Changes

| Area | Change |
|---|---|
| [adapters/ui/tmux.py](../../src/crewplane/adapters/ui/tmux.py) | Route tmux UI to compact runtime |
| [observability/tmux/compact.py](../../src/crewplane/observability/tmux/compact.py) | **[NEW]** 2-pane tmux + Rich Live dashboard |
| [observability/dag_render.py](../../src/crewplane/observability/dag_render.py) | **[NEW]** topological inline DAG renderer with overflow cap |
| [artifacts/directory_manager.py](../../src/crewplane/artifacts/directory_manager.py) | Refactor log file path to node-local structure |
| Runtime execution paths using `get_log_file(...)` | Continue per-invocation logging, now under node-local path |
| [observability/tmux/labels.py](../../src/crewplane/observability/tmux/labels.py) | Include blocked icon and richer status text |

### What doesn't change

- [render/__init__.py](../../src/crewplane/observability/render/__init__.py) remains the non-tmux text renderer.
- [observer.py](../../src/crewplane/observability/observer.py) and [types.py](../../src/crewplane/observability/types.py) observer/snapshot contracts remain unchanged.
- No new dependencies (Rich already in use).

### Visual polish

- Status-colored tmux pane borders (`pane-active-border-style`, `pane-border-style`).
- Unicode status indicators: `✅ done`, `⏳ running`, `⏸ pending`, `❌ failed`, `⛔ blocked`.
- Rich-formatted tmux status bar with progress counters.

### Not included

- Web UI.
- Textual TUI.
- Workflow import implementation.
- Click-to-select-node interaction.
