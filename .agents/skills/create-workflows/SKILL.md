---
name: create-workflows
description: >-
  Creates and revises portable declarative AI workflow files using Markdown
  frontmatter, DAG nodes, provider IDs, artifact references, imports,
  reusable inputs, findings artifacts, and review loops. Use when asked to
  author, improve, modularize, or validate .task.md workflows for CLI-based
  multi-agent workflow orchestration, including parallel fail-safety, prompt-budget
  guards, provider/session resilience, path-based artifact handoffs, and
  generated-file capture.
---

# Create Workflows

## Goal

Create self-contained `.task.md` workflows that coordinate independent AI
provider invocations through explicit files and artifacts. Treat workflow files
as infrastructure: declarative, reviewable, deterministic to validate, and
portable across projects that use the same workflow format.

## Authoring Workflow

1. Inspect the target project before writing.
   - Find existing `.task.md` workflows and the workflow schema version if they
     exist.
   - Reuse provider IDs supplied by the user, already present in existing
     workflows, or configured in the target project. If none are available, use
     clear placeholders such as `planner`, `builder`, and `reviewer` and call out
     that the user must replace them.
   - Do not create or modify provider setup or other non-workflow files.
   - Keep the workflow independent of the skill location and do not reference
     implementation source files as required context.

2. Define the graph before writing prompts.
   - State the workflow goal, final deliverable, and validation gate.
   - Split work into nodes with explicit ownership and artifact handoff.
   - Add `needs` only where data or ordering is required. Independent roots should
     stay parallelizable.
   - Use upstream artifacts for handoff; do not assume providers share memory,
     sessions, or hidden state.
   - Keep each node's context bounded. Prefer artifact paths, concise findings,
     or exact file references over repeatedly inlining large upstream outputs.
   - For research, planning, and design work whose complete result matters to a
     dependent node, pass `{{upstream.output_path}}` and tell the consumer to open
     it. Do not reduce the canonical handoff to findings and risk losing detail.

3. Choose node modes conservatively.
   - Use `mode: parallel` for a normal one-shot invocation with one provider, or
     when multiple executor providers can run the same prompt independently and
     their outputs can be consolidated afterward.
   - Keep multi-provider parallel tasks and independent DAG roots read-only unless
     concurrent mutations are provably disjoint. Add `needs` edges to serialize
     nodes that may edit the same project files.
   - Use single-provider `mode: sequential` only for ordered repeated passes. Its
     `depth` is the total invocation count, and one pass's Markdown output is not
     automatically injected into the next pass. Omitted `depth` means one pass.
   - Use multi-provider `mode: sequential` for executor/reviewer loops.
   - Use `mode: input` for reusable raw-file boundaries. Input nodes have no
     Markdown body section. An input that remains in the composed workflow must
     have `source`; an import-required input may omit it when an importer binds it.
   - Keep DAG concurrency distinct from node mode: independent ready nodes can run
     concurrently even when each node is sequential.

4. Write prompts as contracts.
   - Put stable role, task, constraints, and output format before volatile
     injected artifacts.
   - Delimit large context with headings or XML-style tags so instructions,
     examples, and inputs are unambiguous.
   - Use numbered steps or bullets when order matters.
   - Include concrete output templates for fragile formats.
   - Add few-shot examples only when they materially improve format consistency;
     keep examples relevant and varied.
   - Keep provider-neutral instructions unless the workflow intentionally targets
     a user-specified provider ID with known capabilities.
   - Give downstream nodes exact read scope. If a prior node already inspected
     the project, ask the next node to consume its findings or named files instead
     of performing another broad search.

5. Validate before handing off.
   - Run `crewplane validate PATH` to compile and validate the workflow.
   - Run `crewplane run --dry-run --tasks PATH` to preview the execution plan
     without invoking providers.
   - Both commands are read-only with respect to run artifacts and invoke no
     providers.
   - If validation reports unavailable or unknown provider IDs, report that as an
     external setup requirement. Do not modify files outside the workflow.
   - Fix schema, provider, dependency, template, and role-marker errors before
     considering the workflow complete.
   - If no validator is available, perform the checklist in this skill manually.

## File Shape

Every workflow is one Markdown file with YAML frontmatter followed by one
document-root `## <node-id>` section for each non-input node.

```markdown
---
schema_version: "REPLACE_WITH_TARGET_SCHEMA_VERSION"
name: Workflow Name
description: One concise sentence describing the workflow.
nodes:
  - id: plan
    mode: parallel
    providers: [planner]
  - id: build
    mode: parallel
    needs: [plan]
    providers: [builder]
---

## plan
Create an implementation plan for `{{file:docs/feature-spec.md}}`.
Return scope, likely files, and validation.

## build
Open and implement the complete plan at `{{plan.output_path}}`.
Treat that file as the canonical handoff; do not rely on a compressed summary.

Return changed files, commands run, and remaining risks.
```

## Frontmatter Contract

Common top-level fields:

- `schema_version`: required; match the target project's workflow schema.
- `name`: required human-readable workflow name.
- `description`: optional but recommended.
- `imports`: optional list of reusable workflow files to compose.
- `inputs`: optional map from public input names to `mode: input` node IDs.
- `nodes`: required ordered list of node declarations.

Common node fields:

- `id`: required, lower-case, and stable. Use `[a-z0-9._-]+`; avoid the reserved
  names `logs`, `manifests`, and `workspace-exports`.
- `mode`: `sequential`, `parallel`, or `input`.
- `providers`: required for non-input nodes. Use provider IDs supplied by the
  user or copied from existing workflows.
- `needs`: upstream node IDs this node depends on.
- `source`: for a materialized `mode: input` node, exactly one raw
  `{{file:path}}`. A reusable module may omit it only when the importer must bind
  that input before final workflow validation.
- `findings: true`: enables concise findings extraction for downstream
  `{{node.findings}}` references.
- `depth`: total invocation count for single-provider sequential nodes; for a
  review loop, remediation attempts after the initial candidate in each audit.
- `audit_rounds`: maximum fresh audit passes for multi-provider sequential review
  loops; omit it outside those loops.
- `review_starts_with`: `executor` by default, or `reviewer` for an initial
  pre-review in a multi-provider sequential review loop.
- `continue_on_failure`: mode-specific continuation policy for supported parallel
  invocation failures, reviewer invocation failures, or consensus exhaustion;
  it is not a general exception fallback.
- `failure_threshold`: number of expected invocation failures allowed in a
  parallel node; use only on parallel nodes and keep it below provider count.
- `token_budget`: optional per-node character-threshold overrides for each
  runtime-injected artifact, runtime-resolved file, or prior review candidate.

Provider entries may use shorthand provider IDs. Shorthand always means
`role: executor`; a provider ID does not imply its workflow role:

```yaml
providers: [builder, alternate-builder]
```

Or explicit objects:

```yaml
providers:
  - provider: builder
    model: optional-model-id
    reasoning: optional-provider-native-effort
    role: executor
  - provider: reviewer
    role: reviewer
```

`role: reviewer` is valid only in multi-provider sequential review loops. Place
all executor providers first and all reviewer providers after them. `reasoning`
is a provider-native token. Include it only when the user or an existing workflow
establishes that the selected provider supports it; otherwise omit it.

Workflow keywords are case-sensitive. Keep values such as `mode`, `role`,
`output`, and `findings` lower-case exactly as documented.

## Template References

Use template references to make dataflow explicit:

- `{{file:path}}`: inject a local file. Prefer project-relative paths.
- `{{env:KEY}}`: inject an environment variable.
- `{{var:project_name}}`: inject the target project name when supported.
- `{{param:key}}`: placeholder bound by `imports[].with` during composition.
- `{{upstream.output}}`: inject an upstream node's full result.
- `{{upstream.findings}}`: inject an upstream node's findings artifact.
- `{{upstream.output_path}}` and `{{upstream.findings_path}}`: inject the path to
  an upstream artifact without injecting its contents.
- `{{upstream.output_size}}` and `{{upstream.findings_size}}`: inject artifact
  byte size.
- `{{upstream.output_sha256}}` and `{{upstream.findings_sha256}}`: inject artifact
  SHA-256.

Rules:

- Reference only upstream nodes listed through the DAG.
- `needs` establishes ordering but does not inject content; include the intended
  artifact reference in the prompt.
- Use only the eight supported lower-case artifact names: `output`, `findings`,
  `output_path`, `findings_path`, `output_size`, `findings_size`,
  `output_sha256`, and `findings_sha256`.
- Use `{{node.findings}}` only when the upstream node declares `findings: true`.
- The same requirement applies to `findings_path`, `findings_size`, and
  `findings_sha256`.
- Treat size and SHA-256 references as opt-in metadata, not standard handoff
  fields. Do not add them routinely or merely for completeness.
- Add `*_size` only when the downstream node will make a size-aware decision,
  such as choosing a reading strategy or enforcing an explicit artifact limit.
- Add `*_sha256` only when the downstream node must verify artifact identity or
  integrity, compare copies, or record an explicit audit value.
- When neither condition applies, pass only the artifact path or content needed
  by the downstream task.
- There is no `{{node.generated_files}}` artifact. Captured generated-file links
  are included in the consolidated `{{node.output}}` document.
- Relative `{{file:path}}` references are project-root-relative, including when
  they appear in imported workflows.
- Keep file and environment references intentional; unresolved templates should
  fail validation instead of flowing into provider prompts.

## Context Handoff Resilience

Design workflows so a provider can make progress from a bounded prompt plus
auditable files. Large prompts, repeated full-output injection, and broad
repo-reading instructions increase the chance of provider session stalls,
context compaction loss, or silent non-response.

Choose the smallest handoff that preserves correctness:

- For research, planning, architecture, and design artifacts, default to
  `{{upstream.output_path}}` in dependent nodes and instruct the provider to open
  the complete result. These artifacts often contain connected rationale and
  constraints that findings compression can accidentally discard.
- Use `{{upstream.findings}}` only when a deliberately concise, potentially lossy
  issue or decision summary is sufficient for the downstream task.
- Use `{{upstream.output}}` only when the full upstream result is intentionally
  small or is the canonical artifact the next node must transform.
- Use `output_path` or `findings_path` for file-backed handoffs. Keep the normal
  handoff path-only; add `*_size` or `*_sha256` only when the downstream task has
  an explicit use for that value.
- Use an explicit summarization or extraction node when downstream work needs a
  narrow slice of a large artifact.
- Use prompt-budget fail gates for any node that might receive large upstream
  context. Fail explicitly before invocation rather than silently truncating.

When passing file references, include enough context for the next provider to
read only what matters:

```markdown
Open the canonical plan below as the source of truth. Do not re-scan the project
unless the plan is internally inconsistent.

Canonical plan: `{{plan.output_path}}`

Return the revised plan and a short list of files read.
```

Avoid prompts that combine a massive artifact with open-ended instructions such
as "inspect the entire repository", "use every previous result", or "verify
everything again" unless that is truly the node's job. Split those workflows into
focused audit, extraction, implementation, review, and synthesis nodes.

## Findings Artifacts

Use findings when a downstream node needs concise context instead of the full
result. Findings are intentionally compressed and should not be the sole handoff
for research, planning, architecture, or design work when losing rationale,
alternatives, constraints, or evidence could change the next node's decisions.

```markdown
---
schema_version: "REPLACE_WITH_TARGET_SCHEMA_VERSION"
name: Findings Example
nodes:
  - id: audit
    mode: parallel
    findings: true
    providers: [auditor]
  - id: fix
    mode: parallel
    needs: [audit]
    providers: [builder]
---

## audit

Audit the target area and return a full report.

At the end, include exactly one findings block:
<!-- findings -->
- concise finding with evidence
<!-- /findings -->

## fix

Use only the concise findings:
{{audit.findings}}
```

When `findings: true` is set, require exactly one non-empty findings block in
eligible executor output. Use findings for handoff compression, not hidden
summarization. In mixed executor/reviewer sequential nodes, reviewer outputs do
not participate in findings extraction. Synthetic parallel-failure artifacts are
the exception: Crewplane generates failure findings for those failed executor
invocations.

## Role-Scoped Prompts

Use role markers when executor and reviewer instructions must differ inside one
multi-provider sequential node.

```markdown
## implement.review

Shared task context for both roles.

<!-- crewplane:executor -->
Apply required fixes and return the complete revised candidate.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Inspect the revised candidate for domain-specific correctness risks, regressions,
and missing validation.
<!-- /crewplane:reviewer -->
```

Rules:

- Text outside markers is shared and appears in every scheduled role prompt.
- `executor` and `reviewer` blocks are opt-in role-specific deltas.
- Markers must be standalone HTML comments.
- Marker-like text inside code blocks, lists, blockquotes, or paragraphs is
  literal prompt text.
- Use only `executor` and `reviewer`.
- Do not nest role blocks.
- Do not include empty role blocks.
- Parallel and single-provider sequential nodes allow shared and executor
  segments only; multi-provider sequential review loops also allow reviewer
  segments.

## Reviewer Guidance

Do not paste Crewplane's structured review contract into workflow prompts. The
runtime appends the reviewer-only behavior, current-candidate context, previous
unresolved review state, and required verdict format for review-loop reviewer
invocations.

Reviewer prompt text should focus on task-specific review criteria:

- What correctness, regression, validation, safety, or domain risks to inspect.
- What evidence reviewers should cite when reporting actionable issues.
- Which optional polish is worth mentioning, and what should be ignored.
- Any target-specific acceptance criteria that the generic framework contract
  cannot infer.

Keep reviewer role blocks short. Avoid duplicating generic instructions such as
reviewing only the current candidate, not editing the workspace, or ending with a
specific verdict structure.

## Review-Loop Semantics

A review loop is a multi-provider sequential node with one or more executor
providers followed by one or more reviewer providers.

- Executors run in declaration order and together produce the current canonical
  candidate set.
- Reviewers run concurrently against that candidate. Consensus requires every
  declared reviewer to approve; a failed reviewer never counts as approval.
- `review_starts_with: executor` is the default.
- `review_starts_with: reviewer` performs one initial round-zero review before a
  same-node executor candidate exists. It is valid only for multi-provider
  sequential loops, does not consume `depth`, and still requires an executor to
  produce the canonical node result. Give the reviewer real context through an
  artifact or file reference because `needs` alone injects nothing.
- In a review loop, `depth` is the number of remediation executor attempts after
  the initial candidate in each audit. For example, `depth: 2` permits at most
  three candidate/review cycles per audit. Omitted `depth` defaults to one
  remediation attempt.
- `audit_rounds` is the maximum number of fresh audits. A clean first-pass
  approval stops later audits; a later audit starts from the latest valid
  candidate without carrying forward prior unresolved review state. Omitted
  `audit_rounds` defaults to one audit.
- The consolidated node `output` contains selected canonical executor artifacts
  and the latest selected reviewer artifacts. Findings extraction remains limited
  to executor tasks.
- `continue_on_failure: true` permits continuation after consensus exhaustion.
  With the field omitted or false, exhaustion behavior may still be determined
  outside the workflow, so do not assume it guarantees a fatal result. Reviewer
  invocation continuation does not turn a failed reviewer into approval or make
  executor failures generally recoverable.

## Prompt Budget Guards

Use prompt budgets when a prompt intentionally injects upstream artifact content.
Thresholds count characters, not model tokens, and apply independently to each
referenced artifact, runtime-resolved file, and prior review candidate. They do
not guard the aggregate assembled prompt.

```markdown
---
schema_version: "REPLACE_WITH_TARGET_SCHEMA_VERSION"
name: Budgeted Summary
nodes:
  - id: audit
    mode: parallel
    providers: [auditor]
  - id: summary
    mode: parallel
    needs: [audit]
    providers: [planner]
    token_budget:
      warn_threshold_chars: 20000
      fail_threshold_chars: 40000
---

## audit

Produce the complete audit report.

## summary

Summarize the complete upstream audit injected below:
{{audit.output}}
```

Warn thresholds should record a warning while still injecting the full artifact.
Fail thresholds should abort before provider invocation. Do not silently
truncate, summarize, or replace artifacts unless the workflow has an explicit
node that produces a concise handoff. Within `token_budget`, explicitly set a
threshold to `null` to disable it for that node. Keep all budget choices in the
workflow rather than modifying non-workflow files.

Set budgets on synthesis, finalization, and review nodes that inline large
artifacts. If a node often hits the warn threshold, redesign the handoff before
increasing the limit: use `output_path`, an extraction node, or findings when
intentional compression is safe. An `output_path` reference injects only the path,
not the artifact content.

## Composite and Reusable Workflows

Use imports for reusable workflow modules and input nodes for portable raw input
boundaries.

Import-required reusable module; it is intentionally not standalone because its
public input has no fallback source:

```markdown
---
schema_version: "REPLACE_WITH_TARGET_SCHEMA_VERSION"
name: Reusable Fix Module
inputs:
  review_input: review-input
nodes:
  - id: review-input
    mode: input
  - id: apply
    mode: parallel
    needs: [review-input]
    providers: [builder]
---

## apply

Apply these findings:
{{review-input.output}}
```

Importing workflow:

```markdown
---
schema_version: "REPLACE_WITH_TARGET_SCHEMA_VERSION"
name: Composed Fix Workflow
imports:
  - path: ./reusable-fix.task.md
    as: fix
    inputs:
      review_input: audit
nodes:
  - id: audit
    mode: parallel
    providers: [auditor]
  - id: handoff
    mode: parallel
    needs: [fix.apply]
    providers: [planner]
---

## audit

Produce concise findings that the imported fix workflow can consume.

## handoff

Summarize the composed run:
{{fix.apply.output}}
```

Composition rules:

- `imports[].path` and `imports[].as` are required.
- Relative import paths resolve from the workflow file that declares the import.
  Resolved imports must remain inside project root and must be Markdown files.
- Imported node IDs are referenced through the alias, such as `fix.apply`.
- Imported workflows must use the same schema version as the root workflow and
  the currently supported Crewplane schema.
- Import aliases must match `[a-z0-9._-]+` and be unique in their declaring file.
  Import cycles, fully composed node-ID collisions, and unused `imports[].with`
  keys fail composition.
- `imports[].with` performs literal, one-pass `{{param:key}}` substitution in
  prompt text only. It does not rewrite IDs, dependencies, providers, input
  sources, or other frontmatter.
- `imports[].inputs` binds declared reusable inputs to upstream node IDs and
  rewrites consuming `needs` edges and recognized artifact references.
- Input bindings name upstream nodes, not artifact templates. If the imported
  workflow needs concise content, make the upstream node's output concise or add
  an intermediate node that emits the desired handoff.
- An unbound `{{param:key}}` is rewritten to `{{var:key}}` and must resolve during
  preflight. `project_name` is the built-in runtime variable; bind arbitrary
  values explicitly.
- A bound input node is removed before final validation, so a reusable module may
  omit its fallback `source` when the input is required. Any unbound input that
  remains materialized must have exactly one raw `{{file:...}}` source. Provide a
  valid fallback source when the module should also run standalone.
- Nested imports form alias chains such as `outer.inner.node`. Input targets are
  resolved in the importing workflow's namespace, and the final DAG must still
  make the resolved target upstream of every artifact consumer.

## Common Patterns

DAG fan-out, then synthesis:

```yaml
nodes:
  - id: research.api
    mode: parallel
    providers: [researcher]
  - id: research.ui
    mode: parallel
    providers: [researcher]
  - id: summary
    mode: parallel
    needs: [research.api, research.ui]
    providers: [planner]
```

The independent root nodes can run concurrently. The `summary` prompt should use
the appropriate artifacts explicitly. For complete research, planning, or design
results, open `{{research.api.output_path}}` and
`{{research.ui.output_path}}` instead of compressing either result to findings.
If fan-out nodes may modify overlapping project files, serialize them with
`needs` instead of relying on concurrent execution.

Parallel fail-safety:

```yaml
nodes:
  - id: review.parallel
    mode: parallel
    providers: [auditor-a, auditor-b, auditor-c]
    failure_threshold: 1
    continue_on_failure: true
```

`failure_threshold` is the number of expected provider invocation failures the
node allows; the default is zero. Failures within the threshold are accepted.
Above it, `continue_on_failure: true` permits supported partial completion and
preserves failure artifacts, while unexpected runtime defects still fail.

Executor plus reviewer loop:

```markdown
---
schema_version: "REPLACE_WITH_TARGET_SCHEMA_VERSION"
name: Review Loop
nodes:
  - id: implement.iterate
    mode: sequential
    depth: 2
    audit_rounds: 1
    providers:
      - provider: builder
        role: executor
      - provider: reviewer
        role: reviewer
---

## implement.iterate

Shared context for the current candidate.

<!-- crewplane:executor -->
Implement or remediate the candidate. Return the complete current candidate,
commands run, and an exact link-only `## Generated Files` section when files
changed.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Inspect correctness, regressions, missing validation, and whether the executor
returned a complete candidate.
<!-- /crewplane:reviewer -->
```

Use this when the same node should iterate until reviewers approve or declared
depth is exhausted. Here, `depth: 2` means the initial candidate plus at most two
remediation attempts. Reviewers run concurrently against the current canonical
executor candidate and must all approve; later audit rounds start fresh with
respect to unresolved review state.

## Output Handoff Conventions

Ask executor nodes that change files to include a `Generated Files` section with
links or paths only: an exact `## Generated Files` heading followed by one
concrete workspace-relative path or Markdown link per line. Project-root capture
requires these explicit claims, and each claimed file must be verifiably changed
during that invocation. Crewplane snapshots eligible files and publishes retained
copies under `.crewplane/execution-results/<run-key>/generated-files/<node>/<task>/`,
then appends canonical links to the consolidated node result. Capture is bounded
and best-effort; a capture warning does not replace an otherwise valid provider
result.

For large non-code artifacts, prefer the same pattern: write the artifact to a
stable path, return a concise summary plus link-only references, and make the
downstream prompt name exactly which paths may be opened. Do not copy large file
contents through multiple node outputs.

For Crewplane node results—especially research, plans, specifications,
architecture, and design documents—prefer the built-in `output_path` handoff
rather than asking an executor to duplicate the result into another file. The
dependent node should declare `needs` and open the supplied path as its source of
truth.

When multiple providers produce outputs or findings, expect consolidated results
to follow provider declaration order. Design downstream prompts to consume the
consolidated artifact, not node-local stage filenames.

## Validation Checklist

Before finishing, verify:

- Frontmatter is valid YAML and contains the required schema, name, and nodes.
- Node IDs are unique, match `[a-z0-9._-]+`, are not `.` or `..`, and are not
  reserved names such as `logs`, `manifests`, or `workspace-exports`.
- Every non-input node has exactly one root `## <node-id>` section.
- Input nodes have no Markdown body section. Every input that remains materialized
  after composition has exactly one raw `{{file:...}}` source; a required imported
  input without a fallback is bound and removed.
- Every `needs` entry points to an existing node or composed imported node.
- Every node artifact reference points to a transitive upstream dependency.
- Every findings-family reference targets a `findings: true` node.
- Artifact names are one of `output`, `findings`, `output_path`, `findings_path`,
  `output_size`, `findings_size`, `output_sha256`, or `findings_sha256`.
- Size and SHA-256 metadata is passed only when a downstream node explicitly uses
  it for a size-aware decision, integrity check, copy comparison, or audit value;
  ordinary handoffs use only the needed path or content reference.
- Provider IDs come from the user or existing workflows; placeholders are called
  out for user replacement.
- Provider shorthand is used only for executors; reviewers use explicit object
  form with `role: reviewer`.
- Parallel node providers all use the executor role.
- Parallel `failure_threshold`, when present, is non-negative and less than the
  provider count.
- Sequential `depth` and `audit_rounds`, when present, are positive; `audit_rounds`
  is used only on multi-provider review loops and remains within validator limits.
- Review-loop provider roles are contiguous: executor providers first, reviewer
  providers second.
- `review_starts_with` is used only on a multi-provider sequential review loop,
  and reviewer-first prompts include explicit artifact or file context.
- Every scheduled role renders a non-empty prompt.
- Role markers are standalone, balanced, non-nested, and allowed for the node
  mode.
- Token-budget thresholds are positive when set, and fail thresholds are greater
  than or equal to warn thresholds when both are non-null.
- Token budgets are understood as per-injected-value character guards, not
  aggregate prompt or model-token limits.
- Research, planning, architecture, specification, and design dependencies use
  `output_path` when complete context matters. Findings are used only when
  intentional compression cannot remove decision-relevant information.
- Large upstream artifacts use `output_path`, extraction nodes, or findings when
  compression is explicitly safe instead of repeated full-output injection.
- Downstream prompts that read files name exact paths and scope, and avoid broad
  re-inspection unless the node is explicitly an audit node.
- File and environment templates resolve in the intended execution environment.
- Import paths resolve relative to the declaring workflow and stay inside project
  root; imported file templates remain project-root-relative.
- Import aliases are valid and unique, schemas match, parameters are consumed,
  and composition introduces no cycle or qualified-ID collision.
- Generated-file handoffs use an exact `## Generated Files` section with one
  concrete changed path or link per line, and consumers use result-tree links.
- `crewplane validate PATH` succeeds, or any provider-setup error is reported
  without changing non-workflow files.
- `crewplane run --dry-run --tasks PATH` shows the intended DAG.

## Quality Bar

Prefer small, composable workflows over large all-purpose files. Make dataflow
obvious from `needs` and artifact references. Keep prompts precise enough that a
new provider invocation can perform the task with only the workflow text and the
explicit artifacts injected into it or opened through a supplied artifact path.
