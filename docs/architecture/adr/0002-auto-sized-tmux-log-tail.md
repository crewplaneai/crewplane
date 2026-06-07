# ADR 0002: Auto-Sized tmux Log Tail

## Status
Accepted

## Date
2026-04-10

## Decision
Make the tmux compact dashboard right-pane log tail dynamically auto-size based
on the visible right pane height, while retaining support for a fixed limit
integer.

- `settings.integrations.ui.options.log_tail_lines` accepts `int | null`.
- Omitted or explicit `null` means auto-size.
- Explicit integers keep fixed-cap behavior and are validated as `1..200`.
- Auto-sizing polls `#{pane_height}` during the existing `0.25s` refresh loop
  instead of using tmux resize hooks.
- This policy applies to dashboard mode only. Raw log inspect mode uses
  tmux-native scrolling against the selected real log file and is not capped by
  `log_tail_lines`.

## Context
The previous constant `log_tail_lines: 40` limit in the tmux compact UI was a
hardcoded constraint. On larger monitors, this left awkward empty space, and on
smaller terminals, it could push content off-screen. Users requested dynamic
sizing that respected terminal height and handled window resizes smoothly while
keeping the compact dashboard readable.

tmux already exposes the required primitives. The compact runtime was already
refreshing every `0.25s` and already queried pane width during rendering, so
adding pane height polling keeps the change localized to the tmux observer
instead of changing workflow scheduling, provider invocation, artifacts, or core
runtime execution.

The implementation complexity is low-to-medium: localized code and test changes
inside the tmux UI path, plus documentation updates and optional manual tmux
validation.

## Rationale
1. **Low Architecture Impact**: This is a localized UI change targeting the
   tmux observer, avoiding changes to core runtime or scheduler logic.
2. **Polling over Hooks**: The right pane is already updated continuously by a
   polling loop roughly every `0.25s`. Querying `#{pane_height}` on the same
   loop accommodates resize events without tmux event hooks or extra resize
   state management.
3. **Backward Compatibility**: Allowing `log_tail_lines` to still accept
   integers ensures users who prefer a fixed cap can maintain their workflow.
4. **Clear Mode Boundary**: Compact dashboard mode has synthetic truncation
   rules. Raw inspect mode intentionally delegates full-log navigation to tmux
   history and copy-mode behavior.

## Public Interface
- YAML/config contract:
  `settings.integrations.ui.options.log_tail_lines` is optional and nullable.
- Runtime constructor surface:
  `TmuxCompactRuntime(..., log_tail_lines: int | None = None, ...)`.
- Example config omits `log_tail_lines` by default and shows a commented fixed
  cap example.

## Implementation Notes
- The injected default `40` was removed from config defaults and container
  wiring so the default path resolves to auto mode.
- The tmux UI adapter accepts `None` and validates explicit integer values as
  `1..200`; strings, booleans, and out-of-range integers are rejected.
- Resolved tmux options and `TmuxCompactRuntime` carry
  `log_tail_lines: int | None`.
- Each refresh queries pane geometry with `tmux display-message -p -t <pane>`
  using `#{pane_width}` and `#{pane_height}` for both compact dashboard panes.
- In auto mode, the selected invocation preparation reads up to the current
  right pane height, and rendering keeps only the wrapped log rows that fit
  after already-rendered metadata:
  `available_tail_rows = max(1, right_pane_height - reserved_non_log_rows)`.
- Fixed mode is unchanged: `log_tail_lines: 40` means read and render at most
  40 tail lines before wrapping.
- Very small pane overflow is intentionally not redesigned here. If static
  metadata alone exceeds pane height, existing compact dashboard behavior is
  preserved and only the log tail budget remains responsive.

## Test Coverage
- Config/default tests expect auto mode by default instead of `40`.
- Config tests preserve explicit `null`.
- tmux adapter tests cover omitted, `null`, valid integer, invalid type, and
  out-of-range `log_tail_lines` values.
- Compact runtime tests cover fixed mode, auto mode, and pane resize during
  execution changing visible tail rows on the next refresh.
- The simulated tmux runtime test double responds to both `#{pane_width}` and
  `#{pane_height}` and allows height changes between refreshes.
- CLI/container smoke tests expect `log_tail_lines=None` in the default path.

## Consequences
### Positive
- Improved UX that fully utilizes the available screen real estate automatically.
- Smooth resize handling during execution.
- Extensible configuration model that allows `null` to represent "auto".
- Leaves the separate raw log inspect mode free to use tmux-native scrolling without synthetic truncation rules.

### Negative
- Minor addition of tmux pane height polling during the refresh loop.
- Right pane rendering logic must calculate available lines dynamically by subtracting reserved non-log lines first.

## Rejected Alternatives
1. **Tmux hooks (`window-resized`, `client-resized`)**: Rejected for v1 due
   to unnecessary complexity. The existing polling loop was sufficient to
   handle resizes responsively.

## Follow-ups
None currently.
