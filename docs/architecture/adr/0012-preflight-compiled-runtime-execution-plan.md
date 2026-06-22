# ADR 0012: Preflight-Compiled Runtime Execution Plan

## Status
Accepted and implemented

## Date
2026-06-02

## Implementation Update
2026-06-03

## Decision
Adopt a deterministic preflight compiler that produces the runtime execution contract before any provider invocation.

Preflight is authoritative for workflow syntax, import composition, parameter rewriting, reference resolution, file/env/var policy, dependency edge synthesis, prompt render-plan compilation, runtime-signature inputs, and artifact locator planning. Runtime consumes a `PreflightExecutionPlan`, its materialized execution bundle, runtime services, and same-process `SecretContext`.

Runtime must not parse template tokens, inspect prompt strings for dependency discovery, rerun file/env/var/param policy, infer DAG edges, read original `{{file:...}}` source paths, or consult `WorkflowPlan` for node semantics.

## Context
The previous execution path split template authority across validation, artifact inspection, manifest hashing, and runtime prompt rendering. That created several correctness risks:

- file references could pass validation and then change before runtime read them
- env, var, and param handling could diverge between preflight diagnostics and runtime prompts
- runtime could discover dependency edges by scanning prompt text instead of relying on the validated graph
- duplicate references to the same upstream artifact could create unstable dependency records
- imported workflow file references could resolve against the wrong source root
- duplicate-run detection could include presentation-only UI state or miss execution-affecting config

The project architecture requires blackboard-style orchestration through readable `.orchestrator/` artifacts, deterministic validation, explicit boundaries, and CLI-first provider execution. A compiled execution plan keeps those properties while removing runtime's token and workflow-shape authority.

## Non-Goals
This decision does not introduce:

- vendor SDK orchestration
- hidden cross-node in-memory state
- binary or base64 file-token injection
- node-level incremental caching
- new artifact roots outside the existing execution stage/result layout
- compatibility fallback to runtime template parsing

## Rationale
The selected design makes the workflow compiler, not runtime execution, the source of truth.

1. Preflight failures are deterministic and occur before provider cost is incurred.
2. Runtime behavior is easier to audit because execution is driven by a persisted plan and bundle.
3. Static file substitutions are frozen during preflight, eliminating file-token time-of-check/time-of-use drift.
4. Dependency graphs are stable because edges dedupe by canonical `dependency_signature`, while every token occurrence remains auditable.
5. Workflow idempotency is based on the compiled execution context, not on ad hoc runtime template inspection.
6. Sensitive env, var, and config values can affect signatures without being persisted in raw form.
7. UI and observability remain presentation layers instead of becoming execution-semantic inputs.

## Design Decisions

### Preflight Phase Model
Preflight runs these phases in order:

1. `parse`
2. `frontmatter`
3. `schema`
4. `import`
5. `reference`
6. `node_policy`
7. `file_policy`
8. `env_policy`
9. `var_policy`
10. `template_plan`

Failures write preflight diagnostics, metadata, manifest, and summary artifacts, then stop before provider invocation. `template_plan` is success-only and produces the compiled render plan and execution bundle.

### Plan Contract
`PreflightExecutionPlan` is a deterministic JSON-serializable contract. It contains:

- run and workflow identity
- execution order
- execution nodes
- provider records
- execution policies
- role render streams
- static resource records
- token catalog entries
- dependency graph entries
- artifact contracts
- runtime config snapshot metadata
- workflow and runtime config signatures

Each execution node owns the full runtime contract for mode, findings behavior, dependencies, providers, retry/concurrency policy, token budget, review-loop policy, consensus policy, and artifact locators. Runtime may create and finalize artifacts through those locators, but it must not derive locator shape from the original workflow.

Provider records carry normalized provider, role, model, task id, agent config key, invoker alias, and config signatures. Artifact contracts carry the expected stage, output, findings, log, manifest, and result locators. These are runtime inputs, not hints.

The plan also carries the auditable boundary facts needed by runtime: signed
config snapshots, workflow signatures, provider config fingerprints, static
resource plans, persisted manifest locators, runtime snapshots, redaction
metadata, generated-file validation, reference validation, and signed-plan
verification inputs.

Preflight may first produce a no-run preview for validation, dry-run, and duplicate lookup. The persisted execution plan adds run identity and allocated paths, but the preview and persisted plan must have the same `workflow_signature` because run-specific fields are excluded from that signature.

### Runtime Config Snapshot
Bootstrap creates a side-effect-free `RuntimeConfigSnapshot` before preflight compilation. It resolves and validates configured invoker, artifact, and UI implementations without allocating run directories, starting observers, checking provider executables for run execution, invoking CLIs, or writing files.

The snapshot classifies canonical config and adapter fields by signature scope:

- `execution`: affects provider invocation, scheduling, retries, review loops, outputs, or failures
- `artifact`: affects persisted artifacts, bundle content, manifests, result layout, or file policy
- `observer`: affects only live UI presentation or availability
- `validation`: proves adapter/config checks but does not affect execution unless also scoped as execution or artifact

`effective_runtime_config_signature` includes only execution and artifact scoped fields. Observer-only settings, including `--no-live`, tmux availability, refresh rate, quiet timing, log-tail sizing, and auto-close behavior, do not affect duplicate detection.

Runtime component construction uses the same canonical adapter options represented in the snapshot, so validation, signatures, and execution cannot drift through a second interpretation of adapter config.

### Workflow Signature
`workflow_signature` replaces the earlier `context_hash` semantics for duplicate-run detection. It includes normalized workflow sources, import graph identity, composition inputs, bound param substitutions, unbound param rewrites, canonical dependency graph identity, token/render-plan signatures, static file content hashes, provider execution semantics, HMAC fingerprints for sensitive env/var/config values, and `effective_runtime_config_signature`.

It excludes run-specific fields and observer-only UI state. A successful run with an identical `workflow_signature` is skipped unless `--force` is provided. Duplicate skips do not allocate a new run directory or write run artifacts.

### Template References
Allowed persisted token kinds are:

- `node`
- `file`
- `env`
- `var`

`{{param:key}}` is composition-time only. Bound params are substituted during composition, and unbound params are rewritten to `{{var:key}}` before `var_policy`. `param` cannot appear in the persisted execution plan.

Node references resolve through canonical locators and allowed artifact keys:

- `output`
- `findings`
- `output_path`
- `findings_path`
- `output_size`
- `findings_size`
- `output_sha256`
- `findings_sha256`

Duplicate references to the same canonical upstream artifact in one source node produce one dependency edge. Later occurrences remain in the token catalog for auditability.

Imported workflow inputs remain explicit `child_input_id -> caller locator` bindings. Child modules see only declared child input IDs, and file references resolve relative to the authored child module root before project/allowlist containment checks. `{{<child_input_id>._artifact}}` is not legal template grammar.

Token catalog entries are occurrence-level audit records with source provenance and `token_signature` values. Dependency edges are graph-level records keyed by canonical target locator, artifact key, source node, and `dependency_signature`.

### Static Files
When workspace isolation is disabled, `{{file:path}}` injects UTF-8 text only. Preflight resolves paths against the authored node source root, applies containment policy, reads bytes, rejects undecodable or NUL-containing content, records size and hash, and writes the content into the preflight static bundle.

Runtime consumes only the bundle `content_ref`. It must not read the original `resolved_path` or rerun path policy.

When ADR 0016 workspace isolation is enabled and preflight has a trusted
workspace source snapshot, repo-relative file tokens compile into
`workspace_file_locator` fragments and `workspace_file_locators` plan records.
Project-initial locators are validated with literal Git tree/object reads and
record Git blob identity plus canonical byte digest. Candidate/upstream-source
locators record syntax and containment only and defer blob identity to runtime.
Allowlisted absolute external files remain static resources. The current core
preflight model supports those records only when a trusted workspace source
snapshot is supplied. Enabled core preflight without that snapshot fails closed
instead of reverting repo-relative file tokens to disabled static resources.
The CLI source gate issues that trusted source snapshot after the current
blob_exact source-policy checks pass. Project-initial workspace file locators
are read from Git blob bytes at the recorded base commit. Runtime-dynamic
upstream, reviewer, and remediation locators are resolved from captured result
commits for the current invocation source after an executor candidate has been
captured. Workspace-aware duplicate-skip/resume validates workspace state and
bundle descriptors instead of rereading prompt source bytes from live
workspaces.

### Secrets And Fingerprints
Orchestrator-owned diagnostics, summaries, manifests, plans, runtime snapshots, and generated logs must not leak raw sensitive values.

Sensitive values are represented on disk by redacted metadata, stable HMAC fingerprints, and `value_handle` references. Runtime resolves handles from same-process `SecretContext`. Persisted artifacts alone cannot reconstruct sensitive prompt text, by design.

Environment values are sensitive by default unless explicitly classified non-sensitive. Runtime variable values are non-sensitive by default unless the key matches sensitive naming patterns or explicit metadata marks them sensitive. Non-sensitive values may be stored in the plan for assembly, but diagnostics still redact env and var values.

The stable HMAC key lives at `.orchestrator/preflight/fingerprint.key` and contains 32 random bytes. `orchestrator init` creates it where possible. A real `orchestrator run` may create it only when sensitive fingerprints are needed. `orchestrator validate` and `orchestrator run --dry-run` do not write artifacts or create the key; if no key exists, they use a process-local ephemeral key for preview signatures.

Corrupt, truncated, symlinked, or permission-unsafe persisted keys are deterministic preflight failures. Provider-emitted node output remains outside the orchestrator redaction boundary and is not rewritten by the orchestrator.

The 2026-06-07 boundary hardening update scopes ephemeral fingerprint key state
through preflight compile options/state rather than process-global module state.
Preview signatures remain deterministic within a compile context, and real runs
continue to use the persisted key only when sensitive fingerprints are needed.

### Artifact Flow
Successful runs use the current hyphenated artifact layout:

```text
.orchestrator/execution-stages/<run_key>/preflight/
.orchestrator/execution-stages/<run_key>/preflight/static-files/
.orchestrator/execution-stages/<run_key>/manifests/
.orchestrator/execution-results/<run_key>/
```

`orchestrator run` compiles a no-run preview before duplicate lookup. If compilation succeeds and a duplicate successful manifest exists, the command skips without run allocation. If execution proceeds, one run context is allocated and shared by preflight artifact writing, runtime execution, manifests, logs, and results.

Early parse, frontmatter, schema, or config failures may not have a workflow name. Failure artifacts use `safe_artifact_name(tasks_file.stem)` when available and otherwise `invalid-workflow`; metadata records both the fallback run key and nullable workflow name.

Preflight failure artifacts do not write a final execution manifest and cannot trigger duplicate skips.

`orchestrator validate` and `orchestrator run --dry-run` remain artifact-free and invoke no providers.

Stage, output, findings, log, manifest, and result locators are allocated as
safe artifact paths. Reserved run-root names stay protected, and node-derived
filenames use stage-safe forms so distinct valid node IDs remain distinct.

### Runtime And Observability Boundaries
Runtime assembles prompts from ordered render fragments:

- `literal`
- `static_file_content`
- `workspace_file_locator`
- `static_env`
- `static_var`
- `runtime_locator_lookup`

Runtime may validate the plan schema, verify referenced config signatures, resolve plan locators from execution state, resolve secret handles from `SecretContext`, invoke providers, and finalize artifacts.

Render streams are grouped by runtime target role. Executor streams include authored `shared` and `executor` prompt segments in authored order; reviewer streams include authored `shared` and `reviewer` segments in authored order. Source role is retained for audit, but runtime assembles only from target-role streams.

Runtime must not create dependency edges outside `dependency_graph`, invent artifact keys, or reinterpret assembled prompt text as template syntax.

UI and observability receive a narrow plan-derived topology view. They do not receive the full `WorkflowPlan` or full `PreflightExecutionPlan` as execution authority, and UI adapters are observer-only for this decision.

## Schema Version Evolution
The schema version constant lives in `orchestrator_cli.version`.
Config, workflow, and preflight plan models exact-match the current supported
schema version.

User-authored config and workflow schema-breaking changes must be introduced as
one coordinated change that:

- bumps `SCHEMA_VERSION`;
- updates generated templates and README examples;
- updates CLI validation and preflight diagnostics;
- updates architecture, config, workflow, and template tests;
- records the migration behavior or hard-break rationale in docs.

Compatibility shims should not be added for unsupported schema breaks unless a
new ADR explicitly changes that policy.

## Rejected Alternatives
1. Keep runtime template resolution and pre-run template inspection.

   Rejected because it preserves split authority between validation, manifest hashing, artifact inspection, and runtime prompt rendering. Runtime could still parse tokens, read file paths, discover dependencies from prompt text, or diverge from preflight diagnostics.

2. Compile only prompt render metadata and let runtime keep `WorkflowPlan` semantics.

   Rejected because runtime would still need to infer node mode, providers, findings behavior, retry/concurrency policy, review-loop behavior, consensus settings, and artifact locators from the original workflow. The execution plan must be the complete runtime contract, not an annotation layer over `WorkflowPlan`.

3. Record file-token paths and hashes, then let runtime read the original files.

   Rejected because it leaves a time-of-check/time-of-use gap. Static file substitutions must be materialized during preflight so the prompt content, static resource hash, workflow signature, diagnostics, and runtime input all describe the same bytes.

4. Keep `context_hash` and legacy manifest template-reference collection.

   Rejected because the old identity model was not tied to the compiled execution contract. Duplicate detection must include compiled workflow semantics, static resource content, provider execution semantics, sensitive env/var/config fingerprints, and execution/artifact-scoped adapter config while excluding run-specific fields.

5. Include all runtime configuration in duplicate detection.

   Rejected because observer-only UI settings such as `--no-live`, tmux availability, refresh rate, quiet timing, log-tail sizing, and auto-close behavior do not affect provider prompts, execution policy, or persisted outputs. Including them would create false cache misses and make presentation choices appear execution-semantic.

6. Persist raw secrets or plain hashes to support artifact-only prompt replay.

   Rejected because `.orchestrator/` artifacts are intentionally readable. Sensitive env, var, and config values may affect signatures through HMAC fingerprints and may be injected during same-process execution through `SecretContext`, but raw values and plain hashes must not be persisted.

7. Exclude sensitive env, var, and config values from signatures.

   Rejected because workflows could duplicate-skip even when prompt-affecting or provider-affecting sensitive inputs changed. HMAC fingerprints allow those values to affect identity without exposing them in artifacts.

8. Add node-level incremental caching or dependency-aware partial reruns.

   Rejected because this decision targets deterministic whole-workflow idempotency. Node-level caching would require finer-grained state, invalidation, and replay semantics that conflict with the goal of a simple auditable compiled run contract.

9. Pass the full workflow or full execution plan into UI adapters.

   Rejected because UI and observability are presentation concerns. They receive a narrow topology view so live UI behavior cannot become an execution boundary or mutate duplicate detection.

10. Preserve compatibility fallback to runtime template parsing.

    Rejected because a fallback path would reintroduce the exact split authority this ADR removes. Invalid or unsupported compiled-plan behavior should fail explicitly rather than silently falling back to legacy token resolution.

## Tradeoffs
This design intentionally moves complexity into preflight. The compiler must preserve source provenance, canonicalize imports and references, materialize static resources, scope config signatures, manage redacted fingerprints, and produce a complete runtime contract. That increases the size and responsibility of the preflight boundary, but it removes duplicated runtime policy logic and makes failure behavior deterministic.

The workflow signature is deliberately broad for execution and artifact inputs. Static file changes, sensitive value changes, provider config changes, retry policy changes, concurrency changes, and artifact-affecting adapter options invalidate duplicate skips. This may rerun more workflows than a fine-grained node cache would, but it keeps idempotency auditable and avoids hidden incremental state.

Observer-only UI settings are deliberately excluded from duplicate detection. A run with and without `--no-live` should dedupe the same way because live presentation does not affect provider prompts, execution policy, or persisted outputs. The risk is misclassifying an adapter option; the mitigation is explicit signature scopes and side-effect-free adapter option canonicalization.

Sensitive prompt replay from persisted artifacts alone is intentionally impossible. This protects secrets in readable blackboard artifacts, but it means replaying an exact sensitive prompt requires the original same-process secret context or fresh values that fingerprint identically.

Runtime no longer supports compatibility fallback to template parsing. This is a breaking internal cutover, but preserving both paths would reintroduce split authority and make diagnostics, signatures, and execution behavior harder to prove.

## Consequences
Positive consequences:

- preflight diagnostics are more complete and happen before provider invocation
- runtime execution is simpler to reason about and test
- artifact manifests reflect the compiled execution contract
- imported workflow behavior is deterministic across module roots
- duplicate detection ignores presentation-only UI changes
- sensitive values stay out of persisted orchestrator-owned artifacts

Negative consequences:

- preflight is a larger and more critical compiler boundary
- persisted plan schemas and internal runtime APIs can break during evolution
- exact persisted replay of sensitive prompts is not available without secret context
- whole-workflow duplicate detection remains coarse-grained rather than node-incremental
- adapter option signature scoping must be kept precise as adapters evolve

## Related ADRs
- [ADR 0001: Ports + Adapters Runtime Integrations](0001-ports-adapters-runtime-integrations.md)
- [ADR 0005: Deeper Workflow Validation in Preflight](0005-deeper-workflow-validation.md)
- [ADR 0006: Workflow Composition Primitives](0006-workflow-composition-primitives.md)
- [ADR 0007: Fail Fast on Template Reference Failures](0007-template-reference-failures.md)
- [ADR 0008: Corrupt Manifest Skip Behavior](0008-corrupt-manifest-skip-behavior.md)
- [ADR 0009: Workflow Signature Idempotency and Caching](0009-context-hash-idempotency.md)
- [ADR 0010: Core Positioning - Infrastructure as Code](0010-core-positioning-infrastructure-as-code.md)

## Updates
- **2026-06-07**: Folded in preflight boundary hardening. Ephemeral fingerprint
  keys are scoped through compile state, the schema version constant is
  centralized in `src/orchestrator_cli/version.py`, and future schema-breaking
  changes require coordinated templates, docs, CLI diagnostics, tests, and
  migration notes.
- **2026-06-12**: ADR 0016 initial build pass added disabled-by-default
  workspace policy records and the enabled workspace contract marker. Disabled
  mode keeps ADR 0012 static file resources. Core preflight now models
  `workspace_file_locator` fragments and records for repo-relative enabled file
  tokens when a trusted source snapshot is supplied. The CLI source gate issues
  that snapshot after the implemented blob_exact source checks pass, and runtime
  supports project-initial blob reads, snapshot provider workspaces, mutable
  worktree provider workspaces, deterministic result commits, exported
  bundles, runtime-dynamic upstream/reviewer locator resolution,
  workspace-aware duplicate-skip/resume validation, and cleanup of generated
  workspace paths. Full source-policy hardening and observability remain ADR
  0016 hardening work.
  Explicitly allowlisted absolute external files remain static preflight
  resources.
