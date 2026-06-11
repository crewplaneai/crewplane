# ADR 0001: Ports + Adapters Runtime Integrations

## Status
Accepted

## Date
2026-04-10

## Decision
Adopt a ports-and-adapters runtime architecture for integration points:
- `invoker`
- `ui`
- `artifacts`

Use configuration-driven implementation selection under `settings.integrations` with:
- alias-first resolution,
- dotted-path overrides.

Do not add entry-point discovery or pluggy in this phase.

## Boundary Hardening Update
The 2026-06-07 boundary hardening keeps this ADR as the owning decision record
for runtime integration boundaries. It adds these settled rules:

- Built-in CLI provider behavior is selected by explicit adapter-owned
  capability records and rendered as provider-agnostic invocation plans. Runtime
  execution consumes those plans and does not infer provider behavior from
  executable basenames, CLI flags, output formats, quota text, or usage text.
- Display-only provider log presentation descriptors are also owned by invoker
  adapters. Runtime transports only validated observer metadata, as defined in
  [ADR 0015](0015-display-only-provider-log-presentation.md).
- The built-in CLI invoker owns provider capability records for `claude`,
  `codex`, `copilot`, `gemini`, `kilo`, and `generic`.
- Agent prompt transport defaults to `prompt_transport: "stdin"`. Passing
  rendered prompts through argv requires explicit `prompt_transport: "argv"`
  plus `prompt_transport_arg`, and preflight records warning diagnostics for
  prompt exposure.
- Architecture ports depend on neutral contracts under `architecture/contracts`
  rather than concrete runtime or observability implementation types.
- Built-in adapter options use typed models or `JsonObject`. External
  dotted-path integration options intentionally remain JSON-compatible extension
  payloads, with redaction traversal at that boundary.
- Port contracts remain structural `Protocol`s rather than abstract base
  classes. The supported enforcement path is loading through
  `architecture/loader.py` and the composition root, where aliases, dotted
  paths, factory methods, and adapter capabilities are validated before runtime
  use.
- Boundary tests guard architecture import direction, adapter/runtime
  separation, public API shape, typed boundaries, provider inference ownership,
  prompt transport defaults, source hygiene, and module size limits.
- Public package exports are intentionally narrow, and the package includes a
  PEP 561 `py.typed` marker.
- De facto shared helpers are public APIs or repackaged behind the correct
  module boundary. Guardrails reject cross-module single-underscore imports,
  single-underscore attribute access, and private patch targets.
- Previously oversized orchestration modules are split across focused CLI run,
  workflow parsing/composition, runtime execution, provider invocation,
  observability, artifact, quota, usage, and mock-invoker modules. Module-size
  and source-hygiene checks keep those boundaries from regressing.

## Context
The project is an orchestration layer for AI CLIs, with planned support for API-based invokers and alternative UI surfaces.

The prior structure had meaningful seams but orchestration wiring still hardcoded concrete implementations for output and tmux live runtime selection.

## Rationale
1. Keeps orchestration semantics stable and central.
2. Makes integration replacement explicit and deterministic.
3. Reduces coupling in CLI workflow composition.
4. Enables future LLM API and web UI adapters without scheduler changes.

## Consequences
### Positive
- Cleaner boundaries between orchestration core and integration concerns.
- Better contributor ergonomics for targeted extension.
- Stronger testability through contract-based interfaces.

### Negative
- Additional abstraction and adapter loading code to maintain.
- Additional integration configuration surface to validate and document.

## Rejected Alternatives
1. Keep current direct wiring: insufficient plug-and-play flexibility.
2. Pluggy-based hook system now: unnecessary complexity for current scope.
3. Entry-point discovery now: premature ecosystem overhead.

## Updates
- **2026-04-10**: The architecture is fully implemented with the following built-in aliases:
  - `invoker`: `cli`, `mock` (see [ADR 0003: Mock Invoker Adapter](0003-mock-invoker.md))
  - `ui`: `tmux`, `none`
  - `artifacts`: `filesystem`
- **2026-06-07**: Folded boundary hardening decisions into this ADR. Runtime
  provider inference remains behind the invoker adapter boundary, ports expose
  neutral contracts, and prompt argv transport is an explicit opt-in.
- **2026-06-10**: Added ADR 0015 link for adapter-owned, display-only provider
  log presentation descriptors.

## Follow-ups
1. Add `llm_api` invoker adapter.
2. Add `web` UI adapter.
3. Re-evaluate entry-point discovery when external adapter ecosystem emerges.
