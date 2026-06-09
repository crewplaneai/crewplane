# ADR 0011: Review Execution Workflow Optimization

## Status
Accepted

## Date
2026-04-10

## Scope
This ADR documents the full review-execution workflow optimization: prompt
parsing, role-scoped prompt rendering, prompt-size guardrails, findings
artifacts, spend observability, sequential review-loop state, canonical
candidate selection, and artifact-integrity checks.

## Decision
Adopt one CLI-first review-execution optimization model with six coordinated
parts:

1. Separate provider output correctness from spend telemetry.
2. Parse workflow prompts as ordered role-scoped Markdown segments.
3. Guard prompt-size growth at explicit artifact-injection boundaries.
4. Publish concise findings as explicit first-class artifacts.
5. Run multi-provider sequential nodes as artifact-backed review loops.
6. Finalize review-loop results from runtime-selected canonical candidates and
   monitor provider-caused artifact drift.

The design preserves the blackboard architecture: providers coordinate through
auditable files under `.orchestrator/`, not shared memory, hidden summaries,
provider-native chat state, SDK-specific APIs, or adapter-specific permission
systems.

## Context
Review-oriented workflows had five related problems:

1. Spend visibility mixed visible text estimates with provider token reports and
   implied precision the CLI runtime could not guarantee.
2. Review prompts grew across remediation rounds and placed volatile content
   before stable instructions, which was cache-hostile for providers that
   support prompt caching.
3. Concise downstream context required a visible artifact primitive rather than
   hidden summarization, truncation, or extra model calls.
4. Multi-round review loops carried one concatenated review summary in memory,
   making unresolved state hard to inspect and hard to distinguish from stale or
   resolved feedback.
5. Runtime result selection inferred the final candidate from latest filenames,
   which was brittle when providers emitted status notes, redirected reviewers
   to old artifacts, failed to make progress, or mutated unrelated artifacts.

Role-scoped prompt parsing also became necessary because flat node prompts could
leak executor-only and reviewer-only instructions across roles, while raw-line
Markdown section scanning was fragile around `##` lookalikes in code or prose.

## Goals And Non-Goals
Goals:

- Preserve blackboard orchestration through explicit artifacts under
  `.orchestrator/`.
- Improve sequential review-loop robustness without hidden provider memory.
- Reduce prompt growth by carrying forward only actionable review state.
- Keep review execution generic across code review, documentation review, design
  review, brainstorming review, compliance checks, and other evaluator-style
  workflows.
- Keep provider-specific optimizations optional and isolated from core runtime
  semantics.
- Preserve structured reviewer output parsing and consensus as the baseline
  review contract.
- Keep node-based workflow flexibility rather than introducing a specialized
  review subsystem.

Non-goals:

- Automatic runtime model selection based on executor or reviewer role.
- Built-in domain-specific review logic for code, docs, design, brainstorming,
  or standards checks.
- Hidden summarization passes or extra background model calls.
- Required provider-side file or tool usage as an orchestration contract.
- Default reviewer skipping based only on earlier approval.
- A runtime permission model separate from prompt constraints and artifact-drift
  checks.

Design principles:

- Treat review coordination as explicit artifact-backed state.
- Keep review specialization in prompts, templates, and workflow structure.
- Separate artifact persistence from prompt transport.
- Prefer deterministic inline prompt behavior by default.
- Introduce provider-specific optimizations only behind explicit capability
  gates.

## Workflow Authoring Contract
Workflow nodes can use these review-execution fields:

- `findings: true` opts a node into concise findings extraction.
- `depth` controls local remediation cycles after one fresh audit finds issues.
- `audit_rounds` controls fresh audit passes overall for multi-provider
  sequential review nodes.
- `token_budget` overrides global prompt-budget settings per node.
- Provider `role` is `executor` by default; `reviewer` is valid only in
  multi-provider sequential nodes.

Multi-provider sequential nodes must declare one contiguous executor provider
segment followed by one contiguous reviewer provider segment. Parallel nodes and
single-provider sequential nodes only execute the `executor` role.

The runtime does not automatically choose different models, CLI flags, or
invoker profiles for executor and reviewer roles. Users can express model or CLI
preferences with ordinary provider aliases in configuration and then select
those aliases in workflow nodes.

```yaml
agents:
  codex_deep:
    cli_cmd: ["codex", "exec"]
    default_model: "gpt-5.4"

  codex_fast_review:
    cli_cmd: ["codex", "exec"]
    default_model: "gpt-5.4-mini"
```

Review state is persisted for auditability, but provider transport remains the
compatibility baseline of inline prompt context plus captured provider output.
Providers are not required to read local review-state files directly.

## Role-Scoped Prompt Segments
Workflow node bodies use role markers inside each `## <node-id>` Markdown
section:

```md
## review.node

Shared instructions before block.

<!-- orchestrator:executor -->
Executor-only instructions.
<!-- /orchestrator:executor -->

Shared instructions between blocks.

<!-- orchestrator:reviewer -->
Reviewer-only instructions.
<!-- /orchestrator:reviewer -->

Shared instructions after block.
```

Supported roles are `executor` and `reviewer`. Markers must be standalone HTML
comment blocks:

- Open: `<!-- orchestrator:executor -->`,
  `<!-- orchestrator:reviewer -->`
- Close: `<!-- /orchestrator:executor -->`,
  `<!-- /orchestrator:reviewer -->`

Parsing is local and programmatic. It must not call a provider, LLM, agent, or
external service. Parser tokens here mean Markdown syntax units, not LLM tokens.

The parser uses `markdown-it-py` with the CommonMark preset:

- `heading_open` level 2 plus the following inline text identifies declared
  workflow node sections.
- `html_block` tokens are the only recognized source of role markers.
- fenced code blocks, indented code blocks, inline code, paragraphs, and normal
  text are literal prompt content.
- marker-like text or `## <node-id>` lookalikes inside code or ordinary text do
  not split sections or create role blocks.
- original source slices are preserved instead of rendering Markdown back from
  tokens, so author formatting, comments, whitespace, fenced code, and template
  text stay intact.

Each node body becomes ordered segments tagged `shared`, `executor`, or
`reviewer`. Rendering is:

- executor prompt: all `shared` and `executor` segments in source order.
- reviewer prompt: all `shared` and `reviewer` segments in source order.

Runtime adds execution-loop control text around the rendered workflow-authored
prompt:

- Executor initial or remediation prompt: rendered executor prompt, canonical
  output/remediation instructions, then previous candidate and unresolved review
  state when applicable.
- Reviewer prompt: runtime reviewer safety instructions, rendered reviewer
  prompt as task context, previous unresolved review state when applicable,
  current executor outputs, then the runtime review response contract.

Workflow-authored reviewer segments never replace or precede the runtime
reviewer safety prefix.

Role segment guardrails:

- no YAML prompt DSL
- no provider-specific role parsing behavior
- no custom role support in v1
- no dual or compatibility role syntax
- one parser backend, one marker grammar, and one role-aware renderer path

Validation rejects unknown roles, malformed markers, nested blocks, unmatched or
mismatched close markers, empty role blocks, disallowed role blocks for the
node's execution mode, and empty rendered prompts for any role the node can
actually schedule. Runtime also asserts non-empty scheduled-role prompts after
template resolution.

Composition rewrites `{{node.output}}` and `{{node.findings}}` references inside
every segment, resolves or rewrites `{{param:...}}` inside every segment, and
preserves segment order and role tags across imported workflows. Manifest
hashing includes the composed segment model and collects `{{file:...}}`,
`{{env:...}}`, and `{{var:...}}` references from all segments.

Workflow validation checks template references across every segment. Node
artifact references are valid only for upstream dependencies, `{{node.findings}}`
requires the upstream node to declare `findings: true`, and artifact names are
case-sensitive. After workflow composition, role applicability is validated
against the composed provider roles.

## Prompt Budget Guardrails
Prompt-size controls are explicit character-count guards, not hidden
summarization:

```yaml
settings:
  token_budget:
    warn_threshold_chars: 50000
    fail_threshold_chars: null
```

Nodes can override either field with `token_budget`. Overrides are field-by-field
and can explicitly set a threshold to `null`.

The runtime checks:

- `{{node.output}}` artifact injections.
- `{{node.findings}}` artifact injections.
- review-loop previous-canonical-candidate context.

If the warn threshold is exceeded, the full context is still injected and a
runtime warning is emitted. If the fail threshold is exceeded, the invocation is
aborted before the provider call. No truncation, lossy summarization, or hidden
model pass is introduced.

## Findings Artifacts
Findings are explicit concise artifacts for downstream context:

- A node opts in with `findings: true`.
- Selected executor outputs must contain exactly one non-empty findings block:

```md
<!-- findings -->
concise downstream content
<!-- /findings -->
```

- The consolidated full result remains available through `{{node.output}}`.
- The concise findings result is written as
  `.orchestrator/execution-results/<run>/<node-id>-findings.md` and is available
  through `{{node.findings}}`.
- Result filenames use the node id's stage-safe form, preserving valid node-id
  characters so distinct valid node IDs remain distinct.
- Findings extraction is provider-neutral and happens from captured output, not
  through another model call.

Current implementation detail: when task specs are available, findings
extraction is executor-scoped. Reviewer outputs in mixed executor/reviewer
sequential nodes do not need findings blocks and do not participate in findings
extraction. If task specs are unavailable, extraction falls back to all selected
stage outputs for compatibility.

If extraction is enabled and a selected non-synthetic output has zero, multiple,
or empty findings blocks, finalization raises `FindingsExtractionError`.

When mock invoker `output_mode: "lorem"` is used, findings-enabled invocations
emit one deterministic findings block. Mock `echo` and `file` modes pass content
through unchanged, so fixtures must include the block themselves.

## Provider Invocation And Spend Observability
Each provider call is a fresh CLI subprocess. There is no shared in-memory chat
history between rounds, so later executors and reviewers only know what the
runtime injects into the prompt and what is available through explicit
artifacts.

Spend observability v2 uses two channels:

- The execution output channel decides invocation success/failure and artifact
  content.
- The spend telemetry channel reports capture status, provider usage status,
  visible lower-bound estimates, configured cost, and confidence.

Per invocation telemetry records:

- `attempt_count`
- `cli_captured`
- `output_extraction_status`: `success | missing | malformed`
- `provider_usage_status`: `full | partial | none | malformed`
- `provider_tokens`: `input`, `cached_input`, `cache_write`, `output`,
  `reasoning`, `total`
- `visible_estimate_tokens`
- `visible_estimate_method`
- `visible_estimate_is_lower_bound`
- `configured_cost_usd`
- `invocation_cost_confidence`: `full | partial | none`
- `usage_parse_error`

Aggregate summaries render:

- terminal invocations and attempts
- `CLI invocations captured: X/Y`
- provider token reports as full, partial, and malformed counts
- visible-text estimate, always labeled lower-bound
- configured cost estimate with aggregate confidence:
  `full | partial | none | mixed`

Provider profiles:

- Codex commands require `--json` and `--output-last-message <tempfile>`. The
  node artifact is extracted from the last-message file. Provider usage is
  parsed from JSONL output. Missing or malformed last-message output is fatal.
- Claude commands use `-p` as the effective prompt argument and add
  `--output-format json`. The node artifact is extracted from the JSON `result`
  field. Provider usage is parsed from the JSON `usage` field. Missing or
  malformed result output is fatal. Generated automation templates prefer
  `--bare`, but that flag remains config/template policy rather than a runtime
  invariant.
- Copilot and Kilo are visible-estimate-only until their CLIs expose documented
  machine-readable usage payloads.
- Unknown CLIs use the generic visible-output path.

Classification rules:

- Provider process failure and output extraction failure are invocation failures.
- Usage parsing failures are non-fatal telemetry failures and set
  `provider_usage_status = malformed` with `usage_parse_error`.
- Visible estimates use `ceil(char_count / 4)` and are always lower-bound
  estimates because orchestrator-visible text excludes provider system prompts,
  repo instructions, tool schemas and results, cached context, and reasoning
  tokens.
- For Codex and Claude, required provider buckets are `input` and `output`, plus
  `cached_input`, `cache_write`, or `reasoning` when those prices are configured.
  If `pricing.total` is configured, it is exclusive and becomes the only
  required bucket.
- Missing provider buckets are unknown, not zero, unless a provider contract
  explicitly guarantees omission means zero.
- Invocation cost confidence is `full` when all configured price buckets are
  backed by provider token counts, `partial` when visible estimates fill some
  configured buckets or some configured buckets are missing, and `none` when no
  configured cost can be computed.
- Aggregate confidence is `full` when all invocations are full, `partial` when
  all are full or partial and at least one is partial, `none` when all are none,
  and `mixed` otherwise.

Raw CLI log retention is governed only by `log_cli_output`. The runtime can use
in-memory process buffers and provider-specific temporary files for parsing, but
it does not create a separate durable raw-output channel when logging is
disabled.

### Invocation Boundary Update
The 2026-06-07 boundary hardening keeps review execution provider-neutral while
tightening the invocation layer around it:

- Review verdict constants, sentinels, parsed result data, and render helpers
  live in `core/review_contract.py`. Runtime consensus and the mock invoker
  depend on that core-neutral contract instead of sharing consensus internals.
- Built-in provider prompt/model/output/quota/usage behavior belongs behind the
  invoker adapter boundary. Runtime review execution consumes provider-agnostic
  invocation plans and does not infer provider behavior from executable names.
- Provider processes are started through direct subprocess execution without a
  shell. POSIX runs read the process group with `os.getpgid(process.pid)` after
  spawn and use explicit process group/session cleanup with idle timeout
  handling, pipe-drain grace periods, clean task cancellation, and
  SIGTERM-to-SIGKILL fallback.
- Invocation state transitions use explicit transition/state models with
  exhaustive dispatch. Runtime snapshots and invocation plans are narrower so
  illegal states are harder to represent.
- Retry, quota, provider failure classification, JSON/text provider-error
  parsing, quota reset parsing, and bounded quota retry behavior remain
  explicit invoker/runtime behavior. Diagnostics use condensed log-reference
  context and are covered by regression tests.

## Sequential Review Loop Semantics
A multi-provider sequential node runs as an executor/reviewer loop:

1. Executors produce the current canonical candidate set.
2. The runtime validates that candidate set locally.
3. Reviewers review the current candidate set in parallel within the local
   review step.
4. Reviewer output is normalized into the structured review contract.
5. Unresolved major and minor issues are persisted and carried into the next
   remediation cycle inside the same audit round.
6. If consensus is not reached before local remediation depth is exhausted,
   another fresh audit pass may run, up to `audit_rounds`.

`depth` and `audit_rounds` are separate controls:

- `depth` is the number of remediation fix/verify cycles after a fresh audit
  finds issues. The default is `1`.
- `audit_rounds` is the number of fresh audit passes allowed overall. The
  default is `1`, it is valid only for multi-provider sequential nodes, and it is
  bounded by `settings.max_audit_rounds` (default `5`).

If every reviewer approves during local `round1`, the node ends immediately and
skips remaining configured audit rounds. If consensus is reached only after a
remediation round and more audit rounds remain, the runtime starts the next fresh
audit. Later audit rounds seed the latest canonical candidate as local `round1`
but do not inherit unresolved review state from the exhausted prior audit.

Reviewer prompts always begin with runtime review-only safety instructions and
end with the structured contract:

```md
## Major Issues
None

## Minor Issues
None

## Nitpicks
None

---
VERDICT: CHANGES_REQUESTED | NITS_ONLY | NO_FINDINGS
```

`NITS_ONLY` and `NO_FINDINGS` are approval verdicts. `CHANGES_REQUESTED` is not.
Malformed structured review output is normalized as non-approval and preserved
for inspection; it is not treated as a runtime invocation failure. Format drift
such as leading or trailing text around the structured block is captured in
review metadata and can emit warnings without failing the provider invocation.
During remediation, reviewer prompts emphasize verification of previous
unresolved major/minor issues and regression checking for new issues introduced
or revealed by the current candidate.

Review state artifacts are written under the node stage directory:

- `review-state/<reviewer-task>-round-<n>.state.json`
- `review-state/review-inbox-round-<n>.md` when unresolved major or minor issues
  remain
- reviewer normalized output files
- reviewer raw sidecars with `.raw.txt`
- reviewer metadata sidecars with `.review.json`

Each reviewer state JSON records the reviewer provider, task id, audit round,
local round, approval flag, normalized verdict, evaluation kind, original
verdict, leading/trailing text flags, parsed major issues, parsed minor issues,
parsed nitpicks, unresolved fingerprints, unresolved issue count, normalization
warnings, and the related normalized, raw, and metadata artifact names. The
normalized review markdown becomes the reviewer output artifact; the raw
sidecar preserves the provider's original text.

Each review inbox Markdown artifact records:

- node id and local round context
- unresolved major and minor issues grouped by reviewer
- reviewer source artifacts for unresolved issues
- current executor output paths
- previous executor output paths when a remediation round has prior candidates
- the round goal for the next executor pass

The inbox is a durable blackboard handoff even though the next provider prompt
still receives the relevant unresolved packet inline. Current implementation
writes Markdown inboxes, not a parallel JSON inbox.

When `audit_rounds > 1`, per-audit review artifacts are grouped under
`review-audit-round-N/`, and each audit directory has its own `review-state/`
and local round numbering. Provider logs remain at the node root under
`logs/<provider>/` with audit-aware filenames.

Only unresolved major and minor issues are carried forward. Nitpicks are not
carried unless a reviewer expresses them as major or minor correctness concerns.
Approved reviewer outputs with empty major, minor, and nitpick sections are
preserved as artifacts but do not add primary carry-forward context.
Unresolved fingerprints are computed from normalized reviewer issues. Repeated
unresolved fingerprints after later executor changes emit warning-level stall
diagnostics; they do not automatically fail the node.

All reviewers run for each reviewable current candidate by default, including
reviewers that approved an earlier candidate. Earlier approval is valid only for
the candidate that reviewer saw; later executor changes can introduce
regressions or reveal issues in overlapping reviewer scopes.

Consensus exhaustion behavior:

- If no valid canonical candidate is produced across all audits, the node fails.
- If a valid candidate exists but reviewer consensus is exhausted, the runtime
  persists status and applies `node.continue_on_failure` or
  `settings.sequential_consensus_on_exhaustion` (`continue` by default,
  `fatal` when configured).

## Canonical Candidates And Artifact Integrity
The runtime treats each current-round executor output set as the only canonical
candidate set eligible for the local reviewer step.

High-confidence invalid candidates are rejected before reviewer invocation:

- missing, empty, whitespace-only, or synthetic-failure outputs
- redirect-only or status-note outputs that point reviewers at a different
  `.orchestrator` artifact instead of including the full candidate

The validator is intentionally conservative. Mixed commentary plus a real
candidate, short but plausible candidates, and oddly formatted self-contained
candidates remain reviewable.

Remediation reviewer calls are skipped when the current canonical candidate is
unchanged from the previous canonical candidate after whitespace normalization.
Claims of fixes without identical content still go to reviewers; the runtime
does not attempt semantic diffing of reviewer claims.

Every review-loop node writes
`review-state/review-loop-status.json` at the node stage root. It records:

- node id
- executed audit rounds and final local round number
- consensus and continuation flags
- invalid-candidate, no-progress, and artifact-drift warning counts
- final canonical executor output paths
- reviewer output paths

Stage finalization prefers paths from this status artifact. It falls back to
latest-round filename selection only when the status artifact is absent,
malformed, or references no existing output files. This fallback exists for
compatibility with older or partial runs, not as the primary contract.

Artifact drift checks wrap executor and reviewer invocations:

- Each provider call is allowed to write its own current output path.
- For parallel reviewers in the same local review step, all reviewer output
  paths for that step are allowed.
- Unexpected changes inside the current node stage tree are warning-level drift.
- Changes to execution results, manifests, or reserved run-root log artifacts are
  fatal only when the runtime can attribute them safely to the current
  invocation.
- `logs/summary.md` is strict.
- `logs/events.ndjson` may only gain event records emitted by the guarded
  runtime invocation in attributable windows. Concurrent node windows only reject
  destructive event-log drift so legitimate events from other nodes are not
  blamed on the current provider call.

This keeps the blackboard contract explicit without adding a second permission
model to adapters or config.

## Prompt Transport Strategy
Default review-loop transport is inline and provider-neutral:

- unresolved major and minor issues are embedded in the next executor prompt
- current executor outputs are embedded in reviewer prompts
- previous canonical candidate context is embedded when remediation needs it
- prompts remain self-contained for all current invokers and mock tests

Review-state and review-inbox artifacts are still always written to disk. Those
artifacts improve auditability and future extensibility, but the runtime does
not assume a provider will read them from the filesystem.

Artifact-reference transport remains an optional future optimization. If added,
it must be explicit, capability-gated, and isolated to invoker/provider
configuration rather than baked into the core review protocol. A future surface
could be a review harness option such as `context_transport: "inline" |
"artifact_reference"` or provider capability metadata such as
`supports_workspace_file_reading: true`; the exact configuration shape is
intentionally deferred.

## Result And Artifact Layout
The artifact adapter uses hyphenated run directories:

- `.orchestrator/execution-stages/`
- `.orchestrator/execution-results/`

For each node:

- Full consolidated results are written as `<node-id>-result.md`.
- Findings-enabled nodes can also write `<node-id>-findings.md`.
- Review-loop histories remain under the node stage directory.
- Review-loop finalization uses `review-loop-status.json` before filename
  inference.
- Multiple outputs and findings are ordered by configured provider order when
  task specs are available, then by sorted task id for any leftover files.

## Future Extension Boundaries
Explicit review state creates room for future features without changing the
core orchestration contract:

- deduplication of repeated issues
- explicit disagreement markers
- synthesis nodes over unresolved review state
- richer observability over review-loop state
- optional reviewer scoping and skip semantics
- optional provider-specific artifact-reference transport

These extensions must preserve inline transport as the compatibility baseline
unless and until a workflow or provider capability explicitly opts into a
different mode.

## Consequences
Positive consequences:

- Spend reporting is explicit about capture, provider coverage, lower-bound
  estimates, and cost confidence.
- Output correctness and usage telemetry failures are separated.
- Workflow authors can avoid cross-role prompt leakage and reduce prompt budget
  use by excluding irrelevant role instructions.
- Concise downstream context is selected explicitly through `{{node.findings}}`
  while the full result remains auditable.
- Review loops leave durable review state, raw and normalized reviewer output,
  inboxes, canonical candidates, and status metadata on disk.
- Invalid or unchanged remediation candidates avoid unnecessary reviewer spend.
- Artifact drift is visible without requiring provider-specific transport or
  adapter permission policies.

Negative consequences:

- The spend observability v2 schema and rendered summaries are intentionally
  single-path; there is no compatibility layer or dual rendering path for older
  spend-summary framing.
- Codex and Claude fixtures must track structured CLI output changes.
- Copilot and Kilo remain estimate-only until their CLIs expose documented usage
  payloads.
- Workflow schema and validation are stricter because role segments, findings,
  audit rounds, and token budgets are first-class.
- Findings quality depends on prompt authors requiring the block and on providers
  following it exactly.
- Multi-round review nodes write more artifacts under `review-state/` and,
  when applicable, `review-audit-round-N/`.
- Review-loop execution does more filesystem snapshotting around provider calls.
- Parser and runtime coverage spans core parsing, validation, composition,
  template resolution, runtime prompt assembly, artifact finalization, mock
  invoker behavior, and observability.

## Rejected Alternatives
Rejected alternatives:

1. Keep ambiguous estimated/provider-coverage spend wording.
2. Treat omitted provider token buckets as zero.
3. Make usage parsing failure fail otherwise successful output extraction.
4. Add a generic usage parser or plugin framework before more provider contracts
   exist.
5. Reduce context with hidden summarization, truncation, or extra model calls.
6. Use provider-side file-reading as the default review-loop transport.
7. Skip later reviewer checks merely because an earlier reviewer approval exists
   after executor output changes.
8. Add review-type-specific runtime branches instead of keeping specialization in
   prompts and workflows.
9. Increase `depth` and rely on models to self-correct without canonical
   candidate checks.
10. Add a separate runtime permission system for reviewers and executors.
11. Semantically diff reviewer claims against unresolved issues before every
   review.
12. Use heading-based role syntax such as `### executor`.
13. Add a YAML-only multiline prompt DSL.
14. Keep runtime-only prompt surgery as the main role-control mechanism.
15. Add provider- or adapter-specific role parsing.
16. Support multiple prompt-role syntaxes or custom roles in v1.
17. Add runtime role-aware model or invoker-profile automation.
18. Group review-loop state, transport, and reviewer-skip semantics under a
    generic `token_optimization` feature. Those are review-loop semantics, not
    just token tuning.

## Validation Coverage
The implementation requires deterministic coverage for:

- AST node-section extraction and declared `## <node-id>` matching.
- Interleaved shared, executor, and reviewer role blocks.
- Unknown, nested, unclosed, mismatched, and empty role markers.
- Code-literal immunity for fenced code, indented code, inline code, marker-like
  text, and `##` lookalikes.
- Role-applicability preflight failures for impossible roles.
- Render order and cross-role non-leakage.
- Runtime non-empty scheduled-role assertions.
- Composition rewriting for `{{node.output}}`, `{{node.findings}}`, and
  `{{param:...}}` inside every segment.
- Manifest hash changes for shared and role-specific segment changes.
- Prompt-budget warn and fail behavior for output, findings, and previous
  candidate context injections.
- Findings extraction success and failure cases, including mixed
  executor/reviewer nodes.
- Review-loop state artifacts, review inboxes, unresolved-only carry-forward,
  stall warnings, audit-round grouping, fresh audit restart behavior, and
  parallel reviewer execution.
- Invalid candidate skipping, no-progress skipping, artifact drift warnings and
  fatal drift, status-artifact finalization, and fallback finalization for older
  runs.
- Provider structured output extraction, usage parsing, malformed usage
  telemetry, visible lower-bound estimates, cost confidence, and summary
  rendering.
- Mock invoker end-to-end runs for findings and review-loop artifact behavior.
- Manual multi-round mock runs that inspect
  `.orchestrator/execution-stages/`, confirm unresolved-only carry-forward is
  legible and auditable, and compare prompt size before and after
  unresolved-only carry-forward.

## Implementation Clarifications
The codebase is the source of truth. These details are captured explicitly
because they are easy to misread from the high-level workflow:

- Findings extraction is executor-scoped when task specs are available.
  Reviewer outputs in mixed executor/reviewer nodes do not participate in
  findings extraction.
- Fresh audit rounds are fresh with respect to unresolved review state, but they
  seed the latest canonical executor candidate as local `round1` when a prior
  audit produced one. They do not restart from the original workflow prompt
  output when a later canonical candidate exists.
- `--bare` for Claude is a generated-template preference. Runtime command
  shaping enforces `--output-format json` and the effective `-p` prompt argument,
  but does not inject `--bare` independently of config.
- `review-loop-status.json` is the primary finalization source. Filename-based
  latest-round selection remains only as a compatibility fallback when status is
  missing or unusable.
- Review inboxes are Markdown-only in the current implementation. A parallel
  JSON inbox, an explicit stall failure policy, and reviewer-scope-based
  skipping remain future design questions.
- No source-code discrepancy was found that requires an implementation change.

## Updates
- **2026-04-10**: Accepted spend observability v2, prompt-size guardrails, and
  findings artifacts. Mock `lorem` output was updated to emit deterministic
  findings blocks for findings-enabled nodes.
- **2026-04-20**: Added review-state artifacts, unresolved-only carry-forward,
  and warn-only stall diagnostics.
- **2026-04-22**: Split `audit_rounds` from local remediation `depth`, added
  parallel reviewers per local review step, grouped multi-audit artifacts, and
  made fresh audit passes restart without inherited unresolved state.
- **2026-04-23**: Added canonical candidate validation, remediation no-progress
  skipping, artifact drift monitoring, explicit review-loop status artifacts,
  and result-writer preference for canonical status paths.
- **2026-04-29**: Spend observability v2 became the sole spend observability
  framing.
- **2026-04-30**: Accepted role-scoped prompt segments with AST-based Markdown
  parsing.
- **2026-06-07**: Folded in review and invocation boundary hardening. Review
  contract rendering moved to a core-neutral module, runtime provider inference
  remains behind invoker capabilities, and invocation lifecycle handling uses
  explicit state transitions and subprocess cleanup.

## References
- [ADR 0001: Ports + Adapters Runtime Integrations](0001-ports-adapters-runtime-integrations.md)
- [ADR 0003: Mock Invoker Adapter](0003-mock-invoker.md)
- [Modular Orchestration Architecture](../modular-orchestration-architecture.md)
