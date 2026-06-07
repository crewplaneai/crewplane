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

## Follow-ups
1. Add `llm_api` invoker adapter.
2. Add `web` UI adapter.
3. Re-evaluate entry-point discovery when external adapter ecosystem emerges.
