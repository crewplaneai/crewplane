# ADR 0010: Core Positioning - Infrastructure as Code

## Status
Accepted

## Date
2026-04-10

## Decision
Orchestrator CLI is built as "Control Plane for AI Workflows".
It strictly enforces:
- Declarative `.task.md` files as the source of truth for workflow structure.
- CLI-first execution semantics.
- Vendor-neutral provider orchestration.
- File-based, auditable state.

It is explicitly **not** a general-purpose application framework or provider SDK abstraction.

## Context
The architecture review identified the need to formally state the core positioning so contributors can evaluate new features against the same product boundary. By anchoring the identity to an Infrastructure-as-Code (IaC) model, the project stays focused on declarative workflow definitions, explicit execution, auditable artifacts, and deterministic validation.

## Rationale
1. **Separation of Concerns:** Treating workflows as declarative data (`.task.md`) separates workflow intent from orchestrator implementation details.
2. **Auditability:** CLI-first execution with file-based state keeps every run inspectable. Inputs, intermediate outputs, manifests, and results stay on disk.
3. **Vendor Neutrality:** Providers are selected and invoked through explicit integration boundaries rather than application code depending on a specific vendor SDK.

## Consequences
### Positive
- Clear boundaries on what features belong in the core orchestrator versus external tools or providers.
- Highly reproducible workflows that are friendly to CI/CD pipelines.

### Negative
- Developers looking for an embedded application framework or highly stateful SDK experience will find the declarative boundary rigid.
- The burden of complex state management is shifted to the filesystem rather than in-memory objects, requiring robust file I/O handling.

## Updates
- **2026-04-10**: Decision documented based on architecture review findings to establish the core project identity.
