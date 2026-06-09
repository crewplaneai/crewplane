# ADR 0010: Core Positioning - Infrastructure as Code

## Status
Accepted

## Date
2026-04-10

## Decision
Orchestrator CLI is a **control plane for AI workflows**. It enforces three
constraints:

- **Declarative workflow definitions:** `.task.md` files are the source of truth
  for workflow structure.
- **CLI-first provider invocation:** providers are invoked through explicit
  integration boundaries, never through vendor SDKs or in-process library calls.
- **Filesystem-backed auditable state:** every input, output, manifest, and
  result is inspectable on disk under `.orchestrator/`.

It is explicitly **not** a hosted service, a multi-tenant platform, a provider
SDK abstraction, or an embedded agent framework.

## Context
The architecture review identified the need to formally state the core positioning so contributors can evaluate new features against the same product boundary. By anchoring the identity to an Infrastructure-as-Code (IaC) model, the project stays focused on declarative workflow definitions, explicit execution, auditable artifacts, and deterministic validation.

## Rationale
1. **Separation of Concerns:** Workflow structure lives in declarative `.task.md`
   files, not in application code. The orchestrator compiles, validates, and
   executes those definitions. Features that require runtime code in the
   workflow authoring surface (embedded scripts, inline function definitions,
   programmable hooks) are out of position.

2. **Auditability:** Every run is reproducible from its on-disk artifacts.
   Inputs, intermediate outputs, manifests, and results stay under
   `.orchestrator/`. Features that depend on in-memory state without
   persistence, or that bypass the artifact store, weaken the core contract.

3. **Vendor Neutrality:** Providers are selected through config and invoked
   through adapter boundaries. Provider-specific behavior lives behind those
   boundaries. Features that infer provider semantics from CLI binary names, or
   that add provider-specific code paths in runtime scheduling, are in the wrong
   layer.

## Consequences
### Positive
- Clear boundaries on what features belong in the core orchestrator versus external tools or providers.
- Highly reproducible workflows that are friendly to CI/CD pipelines.

### Negative
- Developers looking for an embedded application framework or highly stateful SDK experience will find the declarative boundary rigid.
- The burden of complex state management is shifted to the filesystem rather than in-memory objects, requiring robust file I/O handling.

## Updates
- **2026-04-10:** Decision documented based on architecture review findings to
  establish the core project identity.
- **2026-06-07:** Confirmed that recent boundary hardening (narrowed public
  exports, `py.typed`, removal of legacy compatibility shims) is consistent with
  this positioning.
