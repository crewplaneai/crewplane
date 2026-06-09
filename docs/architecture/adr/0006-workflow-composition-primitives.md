# ADR 0006: Workflow Composition Primitives

## Status
Accepted

## Date
2026-04-10

## Decision
Introduce workflow composition primitives to declarative `.task.md` files. This allows:
- **Imports:** Using an `imports` block to include other markdown workflows by alias (`as`) and resolving via file paths (`path`).
- **Prompt Parameterization:** Supporting explicit parameter bindings (`{{param:key}}`) mapped through `with` block declarations during an import.
- **Reusable Input Boundaries:** Supporting explicit workflow input declarations (`inputs`) that point to `mode: input` nodes. Input nodes materialize raw file-backed content and allow a workflow to run standalone.
- **Import-Time Input Binding:** Supporting `imports[].inputs` so importing workflows can bind declared imported inputs to upstream node IDs and selectively rewire only the consuming branches.
- **Namespace Isolation:** Automatically prefixing imported nodes using the import alias (e.g., `alias.node_id`) to prevent task ID collisions across the composed workflow graph.
- **Cycle Detection:** Strictly checking and rejecting cyclical import chains.

Composition remains the only phase that resolves `{{param:key}}`. Bound params
are substituted during composition, and unbound params are rewritten to
`{{var:key}}` before preflight reference policy runs. Runtime execution never
sees persisted `param` tokens.

Shared workflow syntax constants and validation diagnostics are centralized so
composition, validation, preflight, manifest signatures, and role-segment
handling use the same node-id, artifact-reference, keyword, and template-token
rules.

Composed workflows continue to preserve DAG semantics: imports are
alias-namespaced, cycles are rejected, dependencies are topologically ordered,
node and invocation concurrency stay bounded by runtime policy, and downstream
artifact references are valid only for upstream dependencies.

## Context
Prior to this decision, the `.task.md` workflow structure had no mechanism for includes, modules, or parameterized templates. Complex orchestrations had to be defined in single files, and integrating standard organizational workflows forced copy-pasting nodes across different repositories. This blocked the registry strategy and forced copy-paste across enterprise projects.

## Rationale
1. **Reuse over Duplication (Phase 2):** Composition is the enabler for code reuse across repositories. Without it, workflows cannot scale beyond single-file copy-paste.
2. **Preserve Declarative Style:** Rather than relying on dynamic programming structures, static imports ensure the graph can still be validated, hashed, and executed deterministically, keeping the declarative nature of `.task.md` intact.
3. **Registry Foundation (Phase 4):** Providing parameterizable templates is the fundamental prerequisite for supporting a community or internal workflow registry system sequence.

## Consequences
### Positive
- Workflows can be modularized and shared.
- Reduced boilerplate and synchronization effort across projects needing similar steps (e.g., common deployment or test tasks).
- Reusable workflows can stay runnable on their own through raw input nodes while still composing cleanly into larger DAGs.
- `imports[].with` remains a small prompt-only mechanism, while `imports[].inputs` handles graph rewiring explicitly.

### Negative
- Increased complexity in workflow loading, parsing, and internal document representation.
- Requires rigorous handling of circular dependencies and parameter validation during composition.
- The compiled graph is larger and slightly harder to debug directly without observability tooling since the executing steps span multiple source files.

## Updates
- **2026-04-11**: Full composition capability implemented via `workflow_composition/__init__.py`, enabling parameterized document rewriting and namespace tracking prior to DAG scheduling.
- **2026-04-11**: Added `mode: input`, top-level workflow `inputs`, and `imports[].inputs`. Bound imported inputs are pruned during composition and rewritten to explicit upstream node dependencies. `imports[].with` remains prompt-only.
- **2026-05-06**: Input-boundary semantics were refined, which separates explicit workflow input declaration from post-composition file materialization and removes the `.orchestrator/inputs` convention.
- **2026-06-07**: Folded in composition boundary hardening. Shared syntax
  constants and structured diagnostics prevent drift between imported workflow
  composition, validation, preflight planning, and runtime execution.
