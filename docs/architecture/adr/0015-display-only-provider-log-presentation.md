# ADR 0015: Display-Only Provider Log Presentation

## Status
Accepted

## Date
2026-06-09

## Decision
Add a display-only log presentation layer for node-local provider logs.

Persisted provider `.log` files remain the canonical audit view. Raw inspect
opens the exact persisted provider `.log` file. Invoker adapters own a small
log-presentation descriptor, runtime transports validated descriptor metadata
as typed observability context, and observability/tmux renders bounded,
sanitized human-readable views when valid metadata is available.

Provider invocation behavior, output extraction, retry/quota handling, usage
parsing, artifacts, manifests, dedupe, workflow/config schemas, and `--no-live`
execution semantics do not change.

## Context
Some built-in provider paths emit structured JSON logs. Those logs are useful
for provider-owned output extraction and usage parsing, but they are noisy in
the compact tmux dashboard.

ADR 0004 keeps live UI fact-based and preserves explicit raw log inspection.
The missing layer is display-only formatting that can make structured logs
readable without turning provider internals into orchestration state.

## Contract
`src/orchestrator_cli/architecture/contracts/invocation.py` owns the descriptor
contract:

```python
LogPresentationFormat = Literal["plain", "json_lines", "json_object"]


@dataclass(frozen=True)
class LogPresentationDescriptor:
    format: LogPresentationFormat
    profile: str = "generic"
```

`format` selects the formatter algorithm. `profile` is an adapter-owned display
hint, normalized with `strip().lower()`. Profiles are rejected only when they
are empty, longer than 64 characters, or outside `[a-z0-9_.-]+`; syntactically
safe unknown profiles render with generic rules.

`AgentInvoker` requires side-effect-free
`log_presentation_for(config) -> LogPresentationDescriptor | None`. Adapter
creation fails when the returned invoker lacks callable `invoke` or
`log_presentation_for`. Per-invocation descriptor failures are observer
fallbacks: runtime emits generic operation
`log_presentation_descriptor_invalid` when telemetry exists and otherwise uses
plain display.

## Built-In Descriptors
Descriptor selection belongs to invoker adapters:

| Invoker | Descriptor |
| --- | --- |
| CLI `claude` | `json_object`, profile `claude` |
| CLI `codex` | `json_lines`, profile `codex` |
| CLI `copilot`, `gemini`, `kilo`, `generic` | `plain`, profile `generic` |
| `mock` | `json_lines`, profile `mock` |

Runtime and tmux must not infer presentation from provider labels, executable
basenames, CLI args, parser profiles, output content, quota/usage parsing,
failure text, or parser output.

## Event And State Flow
Optional `log_presentation_format` and `log_presentation_profile` fields may
appear only in:

- typed runtime/observability event context
- `logs/events.ndjson`
- in-memory dashboard state
- tmux runtime JSON snapshots

They must not appear in provider logs, result artifacts, run or node manifests,
preflight plans, config signatures, workflow signatures, dedupe inputs, or
`logs/summary.md`. A generic descriptor-fallback warning may appear in
`summary.md`, but descriptor values and warning attributes must not render
there.

Reducers preserve descriptor fields once seen; later events without metadata do
not clear them. Descriptor fields are not passed into `InvocationContext`, so
invokers do not receive observer-only presentation metadata during execution.

## Presentation Package
`src/orchestrator_cli/observability/log_presentation/` owns formatting. It
imports the descriptor contract from `architecture/contracts` and does not
import runtime output-extraction modules.

Formatters are bounded and sanitized:

- `plain`: sanitized bounded provider-log tail
- `json_lines`: bounded JSONL records with stderr-prefix handling
- `json_object`: latest bounded attempt body, parsed independently from runtime
  output extraction

The package strips only the recognized initial provider-log header for display.
Persisted logs are not rewritten. Formatter failures, missing logs, invalid
metadata, partial writes, and oversized inputs fall back to sanitized plain
display or bounded notices.

## Tmux Runtime State
Compact tmux selection and inspect state use atomic JSON snapshots:

- `selection-control.json`
- `selected-invocation.json`
- `inspect-invocation.json`

The stale text files `selected-index.txt`, `selected-node-id.txt`,
`selected-log-path.txt`, `inspect-log-path.txt`, and `inspect-node-id.txt` are
intentionally not supported. Compatibility with stale tmux runtime directories
is not required.

Formatted inspect is launched with `sys.executable -m
orchestrator_cli.observability.log_presentation.follow`; raw inspect launches
`tail -n +1 -F -- <log_path>` through an argv-safe launcher after reading the
validated JSON snapshot. Dynamic log paths and descriptor values stay in JSON
snapshots, not shell command strings.

## Raw Log Exactness
Raw inspect shows the exact persisted provider `.log` file. That file is not
guaranteed to be byte-for-byte provider stdout/stderr, because orchestrator log
capture can add an initial header, prefix stderr lines, and normalize retry wait
text. A future byte-exact stdout/stderr archive would be a separate
invoker/process-boundary design.

## Design Tradeoffs
### Adapter-Owned Descriptor Vs UI Heuristics
Putting the descriptor on `AgentInvoker` makes provider presentation an adapter
responsibility, matching the existing rule that provider-specific behavior
belongs behind the invoker boundary. The tradeoff is that every invoker adapter
must implement one more method, and external dotted-path adapters that only
implemented `invoke` must be updated.

The composition root fails fast when an invoker lacks `log_presentation_for`
because that keeps adapter compatibility errors explicit. Individual descriptor
lookup or validation failures during invocation remain observer fallbacks so a
bad presentation hint cannot fail a workflow run.

### Event Context Vs Artifact Or Config State
Descriptor fields travel through typed execution-event context and dashboard
state because they describe a live observer view of one invocation. They are not
written into workflow/config schemas, preflight plans, manifests, signatures, or
dedupe inputs. This keeps presentation changes from changing orchestration
identity.

The cost is a small expansion of the event context surface and persisted
`events.ndjson` records. That is acceptable because the fields are optional,
typed, bounded, and excluded from run summaries except for generic fallback
warnings.

### On-Demand Formatting Vs Persisted Formatted Logs
Formatted views are generated on demand from the canonical provider `.log` file.
This avoids duplicate artifacts, invalidation rules, and ambiguity over which
log is authoritative.

The cost is repeated bounded parsing during dashboard refresh or formatted
inspect. The formatter therefore reads bounded tails, uses parse-size limits,
sanitizes display lines, and falls back to plain display or notices instead of
trying to render complete provider histories.

### Independent Display Parsing Vs Runtime Extraction Reuse
The presentation package parses provider logs independently from runtime output
extraction, usage parsing, retry, quota, and failure classification. This avoids
making UI rendering depend on runtime execution internals or accidentally
turning display failures into invocation behavior.

The cost is some duplicate provider-shape knowledge for display profiles such
as `claude` and `codex`. That duplication is intentionally shallow: it renders
human-readable lines only and does not produce orchestration decisions.

### Atomic JSON Snapshots Vs Legacy Text Files
Tmux runtime state moved to atomic JSON snapshots so selection, inspect view,
descriptor metadata, and freshness counters are written as one coherent record.
This avoids mixed state across several text files and prevents stale keypresses
from opening the wrong log.

The tradeoff is no compatibility path for stale tmux runtime directories using
the older text files. Runtime directories are temporary UI state, so migration is
not required.

## Rejected Alternatives
1. Infer presentation in runtime or tmux from provider names, executable
   basenames, CLI args, output content, parser profiles, quota text, or usage
   parser output. This was rejected because it would move provider-specific
   behavior out of the invoker adapter boundary.
2. Reuse runtime output extraction, usage parsing, quota, or failure parsers for
   dashboard display. This was rejected because those modules make execution
   decisions, while log presentation must be observer-only and failure-tolerant.
3. Persist formatted logs as first-class artifacts. This was rejected because it
   would duplicate canonical provider logs, introduce cache invalidation rules,
   and risk confusing display output with audit output.
4. Add user-authored workflow or config fields for log presentation. This was
   rejected because presentation metadata is derived from the selected invoker
   integration and should not affect schema versions, workflow signatures, or
   dedupe behavior.
5. Keep only raw inspect and no formatted presentation. This preserved exactness
   but left structured provider logs noisy in the compact dashboard, which was
   the gap this ADR addresses.
6. Make formatted inspect the only inspect mode. This was rejected because users
   still need an exact view of the persisted provider `.log` for debugging and
   auditability.
7. Add a byte-exact stdout/stderr archive as part of this ADR. This was rejected
   as a separate process-capture design; this ADR only changes display of the
   existing provider log artifact.

## Consequences
Positive:

- Structured provider logs become readable in compact tmux without changing the
  canonical audit artifacts.
- Provider-specific presentation choice stays at the adapter boundary.
- Missing or invalid presentation metadata cannot affect workflow execution.

Negative:

- Invoker adapters must implement one additional method.
- Observability now owns bounded structured-log rendering code.
