# ADR 0017: Provider-Native Reasoning Controls

## Status
Accepted and implemented

## Date
2026-04-30

## Implementation Update
2026-07-22

## Decision
Introduce an optional `reasoning` field on workflow provider objects:

```yaml
providers:
  - provider: codex
    role: executor
  - provider: codex
    model: gpt-5.6-sol
    reasoning: ultra
    role: reviewer
```

The field accepts a provider-native value: Crewplane passes it through unchanged
and the selected provider defines its meaning. Crewplane does not translate the
value into a shared effort scale.

Crewplane translates `reasoning` into native command-line arguments only for
the built-in `cli` invoker when `provider_kind` is `codex` or `claude`.

Model and reasoning resolution remain independent. A workflow may request
either, both, or neither. A reasoning request does not require an explicit
model, and Crewplane does not define an agent-level reasoning default.

When `reasoning` is omitted or set to `null`, Crewplane emits no reasoning
argument. Provider configuration, raw CLI arguments, session state, and
provider defaults continue to determine behavior. This preserves the existing
invocation path and execution identity for workflows that do not opt in.

## Context
Before this change, workflows could select a model but could not directly state
the desired reasoning effort. Users had to rely on ambient provider settings or
manually add provider-specific CLI arguments. As a result, an
execution-affecting request was not clearly represented in the workflow,
dry-run plan, run records, or duplicate-run identity.

Providers do not share a common reasoning model. Values with similar names may
have different meanings, model compatibility, fallback behavior, or additional
provider-owned effects. Some CLIs expose a per-run option; others use stateful
configuration, model variants, numeric budgets, or no equivalent control.

Crewplane already assigns provider-specific command construction to invoker
adapters and compiles runtime semantics during preflight. This decision extends
those existing boundaries instead of introducing a portable reasoning
abstraction or a separate profile system.

## Goals

- Allow workflows to request provider-native reasoning explicitly.
- Keep provider-specific arguments inside the built-in CLI adapter.
- Detect structural errors and direct configuration conflicts before launch.
- Include an explicit request in plans, run records, and execution identity.
- Preserve existing behavior when `reasoning` is absent.
- Record the exact reasoning value Crewplane sends to each provider.

## Non-Goals

This decision does not introduce:

- named model or reasoning profiles
- a portable `low`/`medium`/`high` reasoning scale
- an agent-level default reasoning value
- first-class reasoning support for every provider kind or invoker
- a generic reasoning compiler or command-line template language
- CLI version probes or a model-and-effort compatibility catalog
- reasoning summaries, raw reasoning, or thinking-block visibility controls
- response verbosity, prompt keywords, or numeric thinking-token budgets
- control over nested provider-owned agents, routing, fallback, clamping, or
  organization policy

## Configuration and Semantic Contract

`ProviderSpec` and its serialized workflow payload carry:

```python
reasoning: str | None = None
```

A non-null value must be an ASCII token between 1 and 64 characters and match
`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`. Crewplane preserves the value exactly as
written. It does not normalize, translate, clamp, or replace it.

The compact string form remains valid, but cannot include a reasoning request:

```yaml
providers: [codex]
```

The selected invoker and configured `provider_kind` determine support. An alias
or executable name does not imply a capability. The current support matrix and
failure behavior are defined in
[Provider Support and Native Transport](#provider-support-and-native-transport).
Crewplane performs reasoning-specific eligibility checks only when the field is
present.

### Omission Delegates Reasoning to the Provider

When `reasoning` is omitted or set to `null`, Crewplane sends no reasoning
override. **The provider** then determines the reasoning behavior through its
normal resolution path, including raw CLI arguments, user configuration,
session state, defaults, and provider policy.

In other words, ***omission delegates the choice to the provider***. It does not
request `none`, `off`, or any particular reasoning level.

### Crewplane Records the Supplied Value

Crewplane records the exact value it sends as `requested_reasoning` in plans,
events, and artifacts. This gives each run an auditable record of the requested
reasoning value without requiring provider-specific output parsing.

## Architecture Boundaries

The feature follows the existing preflight and ports-and-adapters boundaries.

### Core

Core is responsible for:

- parsing and validating the optional workflow field
- carrying `requested_reasoning` through compiled provider records and runtime
  metadata
- including an explicit request in execution identity
- rendering the request in dry-run output and execution records

### Built-in CLI Adapter

The built-in CLI adapter is responsible for:

- declaring support through explicit `provider_kind` capabilities
- validating eligibility and direct conflicts without side effects
- compiling provider-native command arguments
- rejecting duplicate or ambiguous selectors in `cli_cmd`, `extra_args`, or
  directly conflicting environment state
- preserving the existing command when no request is present

### Runtime

Runtime is responsible for:

- passing the compiled request to the selected invoker without interpreting it
- executing the adapter-built argument vector without shell interpolation
- reporting provider failures without lowering, removing, or replacing the
  requested value

## Provider Support and Native Transport

This snapshot records provider behavior verified on 2026-07-16. It explains why
Crewplane initially supports `reasoning` for **Codex CLI** and **Claude Code** only.
Provider products and accepted values may change after that date, so this table
is design context rather than a Crewplane-wide allowlist.

| Provider | First-class `reasoning` support | Known native values or controls | Provider-native transport |
| --- | --- | --- | --- |
| Claude Code | Supported | Model-dependent `low`, `medium`, `high`, `xhigh`, and `max`; composite modes retain their provider-defined meaning | `--effort <value>` |
| Codex CLI | Supported | Documented `minimal` through `xhigh`; model-dependent `max`; `ultra` retains its provider-defined composite meaning | `--config model_reasoning_effort=...` |
| GitHub Copilot CLI | Not supported | `low`, `medium`, `high`, `xhigh`, `max`, and model-specific values | `--effort=<value>` or `--reasoning-effort=<value>` |
| Gemini CLI | Not supported | Model-specific `thinkingLevel` or `thinkingBudget` in stateful model configuration | No direct per-run thinking flag |
| Kilo CLI | Not supported | Provider- and model-specific variants | `--variant=<value>` |
| Generic CLI | Not supported | Integration-specific raw configuration | Existing `cli_cmd` and `extra_args` |

### Supported CLIs: Claude Code & Codex CLI

Crewplane supports the workflow `reasoning` field when both of these conditions
are true:

- the selected invoker is the built-in `cli` invoker
- `provider_kind` is `codex` or `claude`

These are the only combinations that receive adapter-owned validation and the
native transport described below.

#### Claude Code

The adapter emits:

```text
--effort
<requested value>
```

When a workflow sets `reasoning`, the adapter rejects any competing Claude effort
configuration. Conflicting settings include `--effort` in `cli_cmd` or `extra_args`,
and a non-empty `CLAUDE_CODE_EFFORT_LEVEL` inherited from the environment or supplied
through `--settings`.

`CLAUDE_CODE_EFFORT_LEVEL` is defined by Claude Code rather than Crewplane. Users do
not need to set it; this check applies only when the variable is already set.

#### Codex CLI

The adapter emits:

```text
--config
model_reasoning_effort="<requested value>"
```

When a workflow sets `reasoning`, the adapter rejects an existing
`model_reasoning_effort` setting in `cli_cmd` or `extra_args`.

`model_reasoning_effort` is defined by Codex CLI rather than Crewplane. Users do not
need to set it; Crewplane generates it automatically from the workflow
`reasoning` value.

Crewplane validates the request's structural safety and direct configuration
conflicts. The provider validates whether the native value is recognized and
compatible with the selected model.

### CLIs Not Yet Supported

Crewplane does not currently provide first-class `reasoning` support for
`copilot`, `gemini`, `kilo`, `generic`, or third-party invokers. Setting
`reasoning` for one of these combinations fails during validation or preflight.

The unsupported built-in CLI provider kinds can still use `cli_cmd` and
`extra_args` for their native options. Crewplane passes those options through
as raw adapter configuration without treating them as a workflow `reasoning`
request or recording them as `requested_reasoning`. Third-party invokers remain
responsible for their own configuration contract.

## Persistence

An explicit `requested_reasoning` value appears in:

- the compiled preflight execution plan
- `crewplane run --dry-run`
- invocation-start metadata and provider log headers
- the execution identity used by resume and duplicate-run detection

When the field is absent, Crewplane omits it from persisted and rendered data
where possible. Existing workflows therefore retain their previous signatures
and record shapes.

## Failure Behavior

Invalid values, unsupported integrations, duplicate native selectors, and
direct Claude environment conflicts fail before the provider starts. Unknown
values and model incompatibilities may fail after launch because Crewplane does
not maintain a compatibility catalog.

Existing retry policy remains authoritative. Every attempt uses the same
reasoning arguments. Crewplane never recovers by changing or removing the
request, selecting another model, or falling back to an unmanaged default.

## Tradeoffs

### Native fidelity over portability

Passing values through unchanged preserves each provider's vocabulary and
allows providers to add values without requiring a Crewplane schema release.
The cost is that a workflow value is not portable across providers and may
carry semantics beyond a simple effort level.

### Stable boundary validation over exhaustive compatibility checks

Crewplane can deterministically validate value shape, adapter eligibility, and
direct configuration conflicts. It leaves model-and-value compatibility to the
provider. This avoids a fast-moving compatibility catalog, but moves some
failures from preflight to provider invocation.

### Explicit workflow intent over centralized defaults

Keeping reasoning on each workflow provider makes the request visible and keeps
precedence rules simple. Workflows may repeat the same value across several
provider entries, and ambient provider configuration still lacks the
provenance of an explicit request.

### Narrow first-class support over speculative abstraction

Supporting two concrete CLI transports keeps provider behavior behind the
adapter boundary without inventing an unproven extension model. Other CLIs must
continue to use raw configuration and receive neither first-class validation
nor `requested_reasoning` provenance.

### Auditable inputs over provider-state introspection

Crewplane records the exact value supplied to the provider instead of parsing
provider output to reconstruct internal state. This keeps records deterministic
and avoids coupling core orchestration to provider-specific output formats.

### Backward-compatible omission over fully controlled execution

Emitting no argument when the field is absent preserves existing commands,
signatures, and defaults. Such executions may still depend on ambient provider
configuration, so omission is less reproducible than an explicit request.

## Consequences

### Positive

- Codex and Claude workflows gain one small, auditable configuration field.
- Provider-specific flags remain behind the built-in invoker boundary.
- Plans and run records state what Crewplane requested.
- Present reasoning requests participate in resume and duplicate-run identity.
- New provider-native tokens do not require a Crewplane schema change.
- Workflows that omit reasoning retain existing behavior.

### Negative

- Reasoning values are intentionally not portable.
- Invalid or model-incompatible values may fail only after provider launch.
- First-class support and provenance are limited to selected built-in CLI
  provider kinds.
- Adapter conflict detection must evolve if native CLI transports change.

## Rejected Alternatives

### Named model and reasoning profiles

A single optional field does not justify a profile catalog, another set of
precedence rules, or profile-specific execution identity. Workflows can pair
`model` and `reasoning` directly when they need both.

### Portable Crewplane reasoning levels

Providers and models do not assign consistent meaning or support to similarly
named levels. A Crewplane-owned mapping would create false portability and
would require Crewplane to define fallback or clamping behavior.

### Agent-level default reasoning

An agent-level default would add another precedence layer among workflows,
agent configuration, raw arguments, and provider configuration. Leaving the
field unset preserves existing agent and provider behavior.

### First-class support for every CLI

Crewplane has no dependable transport or semantic contract that spans every
provider. Other integrations can continue to use `cli_cmd`, `model_arg`, and
`extra_args` for provider-specific options.

### Capability catalogs and CLI version probes

Maintaining model-and-value compatibility across CLI versions would add a large,
fast-changing policy surface. Providers remain the authority on which values
their models support.

### Treat omission as `none` or as a known provider default

Raw arguments, persisted settings, aliases, session state, and organization
policy may still select reasoning behavior. Omission can describe only what
Crewplane adds to the invocation.

### Infer reasoning state from provider output

Provider output formats are not stable cross-provider contracts. Parsing them
to reconstruct internal reasoning state would couple core orchestration to
provider-specific presentation, while the workflow and run record already
capture the exact value Crewplane supplied.

### Add a generic reasoning compiler or invoker extension contract

Two concrete CLI transports do not establish a stable cross-invoker
abstraction. A generic contract should follow a demonstrated third-party
integration need.

### Infer support from aliases, executable names, or arguments

Names and command text are user-configurable, so they are not reliable
capability declarations. Support follows the selected invoker and the
adapter-owned `provider_kind`.

## Related Decisions

- [ADR 0001: Ports + Adapters Runtime Integrations](0001-ports-adapters-runtime-integrations.md)
- [ADR 0012: Preflight-Compiled Runtime Execution Plan](0012-preflight-compiled-runtime-execution-plan.md)

## User Documentation

- [Provider setup](../../getting-started/provider-setup.md)
- [Workflow syntax](../../reference/workflow-syntax.md)
