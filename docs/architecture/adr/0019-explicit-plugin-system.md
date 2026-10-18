# ADR 0019: Explicit Plugin System

## Status

Accepted

## Date

2026-10-14

## Decision

Support separately packaged Python plugins through explicit configuration at
existing extension boundaries. Plugins implement documented public contracts;
the component that owns each contract validates and loads its implementations.
Adapter-hosted plugins can compose with built-in behavior without changes to
core orchestration or the built-in registry.

Keep plugin selection auditable, bind execution-affecting registrations to
existing execution signatures, and preserve the host's lifecycle guarantees.
Crewplane remains CLI-first, declarative, and artifact-backed.

## Context

Existing ports allow external packages to replace entire integrations. Some
extensions need to contribute behavior within an adapter while reusing its
planning, supervision, and output handling. Requiring whole-adapter replacement
for those extensions duplicates responsibilities and makes composition harder.

Independent package releases also affect reproducibility: execution reuse must
distinguish plugin versions, and loading an extension must not change scheduler,
artifact, or resource ownership contracts.

## Rationale

1. Explicit configuration makes the active extension set reviewable and prevents
   installed packages from silently changing execution behavior.
2. Existing ports and adapter-owned contracts keep extension behavior local
   while preserving shared orchestration and lifecycle machinery.
3. Canonical package identity makes dedupe and resume sensitive to plugin
   changes without introducing a parallel execution or compatibility system.

## Design Decisions

### Extension Boundaries

Keep adapter selection in the existing integration configuration. Additional
CLI families use the invoker's public capability contract; different invocation
transports use the invoker port. Provider-specific validation, planning, output
interpretation, and failure classification remain inside the invoker boundary.
Runtime executes and supervises provider-neutral plans, and architecture ports
do not depend on concrete runtime or observability implementations.

UI extensions retain optional, observer-only delivery. They do not provide
reliable notifications or authoritative business actions. Real execution
continues to require the filesystem artifact backend. New delivery or external
side-effect requirements need a defined contract before another port is added.

The plugin registry is process-local wiring. Authoritative execution inputs
remain in compiled plans, and cross-node communication remains in verified
artifacts. Plugins inherit existing workflow, workspace, reasoning, review, and
platform guarantees.

### Registration And Trust

Adapter-hosted capability registrations use extensible, validated identifiers
while reserving built-in names. Each registration explicitly names both its
implementation and supplying distribution; import names do not establish
distribution identity. Require installed distribution metadata, validate the
exported contract, and match its declared identity to the registration before use.

Load only configured plugins, or an official onboarding recipe explicitly
selected by the user to prepare that configuration. Onboarding may check known
package metadata before selection; it must not import arbitrary installed
packages. Unknown identifiers, missing packages, import
failures, invalid exports, and identity mismatches fail explicitly before
invocation; never substitute a generic or alternate implementation. Each
configured host binds an immutable registration set without mutating the global
built-in registry.

Plugins are installed into Crewplane's Python environment. Imports execute
trusted Python code and must be free of side effects by contract. Registration
validation is not a sandbox or a package provenance guarantee.

The official Ollama package is a required dependency of standard Crewplane
installs. Installation does not activate it: selection or explicit registration
still controls loading, and the user owns the Ollama server and models.

### Canonical Identity And Reuse

Authored capability registrations declare implementation and distribution.
Canonical registrations additionally record the resolved installed version.
Preflight validation and runtime construction use the same canonical records.
Construction verifies the expected contract identity, distribution identity,
and version; it must reject a mismatch instead of refreshing the recorded identity.

The complete configured capability registration set participates in existing
dedupe and resume signatures, including registrations unused by the workflow.
Observational UI settings remain outside execution identity. Plugins must not
depend on options excluded from their execution signatures.

Reuse the existing signature algorithm, manifests, and artifact layout.
Distribution versions do not detect unversioned source edits or changes to
external resources under unchanged identifiers; canonicalization does not hash
package source trees or external resource contents.

### Lifecycle And Availability

Plugins must support overlapping requests without shared mutable request state.
Planning owns allocated resources until a complete plan transfers ownership to
runtime; failures before transfer release those allocations. Runtime owns
cleanup after success, failure, or cancellation. Output consumers borrow
captures and must respect their owner's lifetime.

Optional availability checks belong to the invoker boundary, separate from
registration resolution. Validation and real-run preflight await these checks
for the effective request, including workflow overrides. Canonicalization and
dry-run perform no probes; validation and dry-run allocate no run artifacts.

Optional onboarding discovery also belongs to the invoker boundary. Discovery
is read-only and bounded; the CLI owns choices and configuration writes.
Selected endpoint settings become explicit agent environment values, shared by
validation and execution and included in existing execution signatures. Runtime
must not rediscover or silently substitute endpoints.

Checks use bounded, supervised work and finish owned cleanup before returning
or propagating cancellation. Unresolved cleanup remains an explicit failure
with diagnostic evidence.

Invocation ownership does not extend to independently managed external
services. Availability changes after validation follow normal invocation failure
and retry contracts; plugins do not weaken output validation, successful
publication, or downstream failure handling.

### Compatibility

Use public extension contracts, loader validation, package versions, and normal
Python dependency constraints for compatibility. Keep authored schema policy
under the existing shared schema version. Introduce no separate plugin API
version, compatibility adapter, migration layer, or parallel legacy API.

## Non-Goals

- Automatic discovery of arbitrary plugin packages, a plugin marketplace or installer, hot reload, or runtime
  dependency negotiation.
- General-purpose workflow hooks, new node types, or hidden cross-node state.
- Provider provisioning, daemon management, or external resource scheduling.
  Node concurrency and per-invocation concurrency retain their existing meanings.
- New invocation features, delivery guarantees, or platform support solely
  because an integration is packaged as a plugin.

## Rejected Alternatives

- **Automatic discovery and a general-purpose hook framework.** These make the
  active extension set less explicit and introduce lifecycle machinery beyond
  the supported extension boundaries.
- **Whole-adapter replacement for every extension.** This forces packages to
  reproduce responsibilities already owned by an existing adapter.
- **Refresh package identity during execution.** The recorded identity could
  diverge from the implementation loaded after preflight.
- **Sign only used registrations or hash package contents.** These introduce
  selective dependency tracking or a separate content-identity policy beyond
  the existing configuration and package-version contract.

## Consequences

### Positive

- Packages can be distributed independently and compose with built-in behavior
  while preserving scheduling, artifact, dedupe, and resume contracts.
- Extension ownership, active registrations, and package versions remain
  explicit and auditable.
- Shared lifecycle machinery applies consistently to built-in and external
  implementations.

### Negative

- Operators must install and trust compatible packages in Crewplane's Python
  environment; plugin authors must maintain compatibility with public contracts.
- A configured package upgrade invalidates reuse even when its registration is
  unused. Unversioned changes remain outside package identity.
- Availability checks may consume external resources and cannot guarantee
  continued availability or cancellation of independently managed work.
- Extensions remain limited by their host's contract; new delivery semantics
  require an architectural decision before an additional extension point.

## Related Decisions

- [ADR 0001: Ports, Adapters, and Runtime Integrations](0001-ports-adapters-runtime-integrations.md)
- [ADR 0010: Core Positioning - Infrastructure as Code](0010-core-positioning-infrastructure-as-code.md)
- [ADR 0012: Preflight Compiled Runtime Execution Plan](0012-preflight-compiled-runtime-execution-plan.md)
- [ADR 0013: Version Source of Truth and Documentation Drift Reduction](0013-version-source-of-truth-and-documentation-drift-reduction.md)

Supported extension contracts and configuration are documented in the
[integration reference](../../reference/integrations.md).
