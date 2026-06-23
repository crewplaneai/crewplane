---
name: create-workflows
description: >-
  Creates and revises portable declarative AI workflow files using Markdown
  frontmatter, DAG nodes, provider aliases, artifact references, imports,
  reusable inputs, findings artifacts, and review loops. Use when asked to
  author, improve, modularize, or validate .task.md workflows for CLI-based
  multi-agent workflow orchestration, including parallel fail-safety, prompt-budget
  guards, provider/session resilience, and generated-file handoffs.
---

# Create Workflows

## Goal

Create self-contained `.task.md` workflows that coordinate independent AI
provider invocations through explicit files and artifacts. Treat workflow files
as infrastructure: declarative, reviewable, deterministic to validate, and
portable across projects that use the same workflow format.

## Authoring Workflow

1. Inspect the target project before writing.
   - Find existing `.task.md` workflows, configured provider aliases, and the
     workflow schema version if they exist.
   - Reuse local provider aliases exactly as configured. If none are discoverable,
     use clear placeholders such as `planner`, `builder`, and `reviewer` and call
     out that they must match the target project's config.
   - Keep the workflow independent of the skill location and do not reference
     implementation source files as required context.

2. Define the graph before writing prompts.
   - State the workflow goal, final deliverable, and validation gate.
   - Split work into nodes with explicit ownership and artifact handoff.
   - Add `needs` only where data or ordering is required. Independent roots should
     stay parallelizable.
   - Use upstream artifacts for handoff; do not assume providers share memory,
     sessions, or hidden state.
   - Keep each node's context bounded. Prefer concise findings, manifests, or
     exact file references over repeatedly inlining large upstream outputs.

3. Choose node modes conservatively.
   - Use `mode: sequential` as the default for one provider or an ordered
     executor/reviewer loop.
   - Use `mode: parallel` when multiple providers can run the same task
     independently and their outputs can be consolidated afterward. Parallel
     providers must all be executors.
   - Use `mode: input` for reusable file-backed input boundaries. Input nodes
     have `source` and no Markdown body section.

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
     a provider alias with known capabilities.
   - Give downstream nodes exact read scope. If a prior node already inspected
     the project, ask the next node to consume its findings or named files instead
     of performing another broad search.

5. Validate before handing off.
   - Run the project's workflow validator or dry-run command when available.
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
    mode: sequential
    providers: [planner]
  - id: build
    mode: sequential
    needs: [plan]
    providers: [builder]
---

## plan
Create an implementation plan for `{{file:docs/feature-spec.md}}`.
Return scope, likely files, and validation.

## build
Implement the plan:
{{plan.output}}

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

- `id`: required, lower-case, and stable. Use `[a-z0-9._-]+`; avoid `logs` and
  `manifests`.
- `mode`: `sequential`, `parallel`, or `input`.
- `providers`: required for non-input nodes. Use configured aliases.
- `needs`: upstream node IDs this node depends on.
- `source`: required only for `mode: input`; exactly one raw `{{file:path}}`.
- `findings: true`: enables concise findings extraction for downstream
  `{{node.findings}}` references.
- `depth`: repeat count for single-provider sequential executor passes, or
  remediation fix/verify cycles after a fresh audit in a review loop.
- `audit_rounds`: fresh audit passes for multi-provider sequential review loops;
  omit it outside those loops.
- `continue_on_failure`: allows supported partial-failure or exhausted-consensus
  cases to continue.
- `failure_threshold`: partial-failure threshold for parallel work; use only on
  parallel nodes and keep it below the provider count.
- `token_budget`: optional per-node prompt-size guard overrides.

Provider entries may be shorthand aliases:

```yaml
providers: [builder, reviewer]
```

Or explicit objects:

```yaml
providers:
  - provider: builder
    model: optional-model-id
    role: executor
  - provider: reviewer
    role: reviewer
```

`role: reviewer` is valid only in multi-provider sequential review loops. Place
all executor providers first and all reviewer providers after them.

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

Rules:

- Reference only upstream nodes listed through the DAG.
- Use lower-case artifact names: `output` and `findings`.
- Use `{{node.findings}}` only when the upstream node declares `findings: true`.
- Keep file and environment references intentional; unresolved templates should
  fail validation instead of flowing into provider prompts.

## Context Handoff Resilience

Design workflows so a provider can make progress from a bounded prompt plus
auditable files. Large prompts, repeated full-output injection, and broad
repo-reading instructions increase the chance of provider session stalls,
context compaction loss, or silent non-response.

Choose the smallest handoff that preserves correctness:

- Use `{{upstream.findings}}` for concise issues, decisions, and evidence.
- Use `{{upstream.output}}` only when the full upstream result is intentionally
  small or is the canonical artifact the next node must transform.
- Use file-backed handoffs for large generated content, logs, plans, reports, or
  source snapshots. Pass a short manifest containing path, producer, purpose, and
  size or hash when the format supports it.
- Use an explicit summarization or extraction node when downstream work needs a
  narrow slice of a large artifact.
- Use prompt-budget fail gates for any node that might receive large upstream
  context. Fail explicitly before invocation rather than silently truncating.

When passing file references, include enough context for the next provider to
read only what matters:

```markdown
Use the handoff manifest below as the source of truth. Open only the listed
files needed to answer the task, and do not re-scan the project unless the
manifest is internally inconsistent.

## Handoff Manifest
- `artifacts/audit-findings.md`: concise findings from the audit node.
- `artifacts/current-plan.md`: current canonical plan to revise.

Return the revised plan and a short list of files read.
```

Avoid prompts that combine a massive artifact with open-ended instructions such
as "inspect the entire repository", "use every previous result", or "verify
everything again" unless that is truly the node's job. Split those workflows into
focused audit, extraction, implementation, review, and synthesis nodes.

## Findings Artifacts

Use findings when a downstream node needs concise context instead of the full
result.

```markdown
---
schema_version: "REPLACE_WITH_TARGET_SCHEMA_VERSION"
name: Findings Example
nodes:
  - id: audit
    mode: sequential
    findings: true
    providers: [reviewer]
  - id: fix
    mode: sequential
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
not participate in findings extraction.

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

## Prompt Budget Guards

Use prompt budgets when upstream artifacts can grow large.

```yaml
nodes:
  - id: summary
    mode: sequential
    needs: [audit]
    providers: [planner]
    token_budget:
      warn_threshold_chars: 20000
      fail_threshold_chars: 40000
```

Warn thresholds should record a warning while still injecting the full artifact.
Fail thresholds should abort before provider invocation. Do not silently
truncate, summarize, or replace artifacts unless the workflow has an explicit
node that produces a concise handoff.

Set budgets on synthesis, finalization, and review nodes that consume several
upstream artifacts. If a node often hits the warn threshold, redesign the handoff
before increasing the limit: add findings, an extraction node, or a file-backed
manifest.

## Composite and Reusable Workflows

Use imports for reusable workflow modules and input nodes for portable raw input
boundaries.

Reusable module:

```markdown
---
schema_version: "REPLACE_WITH_TARGET_SCHEMA_VERSION"
name: Reusable Fix Module
inputs:
  review_input: review-input
nodes:
  - id: review-input
    mode: input
    source: "{{file:.workflow-inputs/review-findings.md}}"
  - id: apply
    mode: sequential
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
  - path: workflows/reusable-fix.task.md
    as: fix
    inputs:
      review_input: audit
nodes:
  - id: audit
    mode: sequential
    providers: [auditor]
  - id: handoff
    mode: sequential
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
- Keep import paths project-relative and inside the target project's allowed
  workflow boundary.
- Imported node IDs are referenced through the alias, such as `fix.apply`.
- Imported workflows should use the same schema version as the root workflow.
- `imports[].with` binds `{{param:key}}` text only; it does not rewire DAG edges.
- `imports[].inputs` binds declared reusable inputs to upstream node IDs and
  rewires only consuming branches.
- Input bindings name upstream nodes, not artifact templates. If the imported
  workflow needs concise content, make the upstream node's output concise or add
  an intermediate node that emits the desired handoff.
- Unbound `{{param:key}}` tokens are composition-time placeholders. Do not rely
  on them as runtime inputs; bind them with `imports[].with` or model them as
  explicit input nodes.
- Partially bound reusable inputs may leave unbound input nodes file-backed so
  the imported workflow can still run standalone.
- Avoid alias and node ID collisions.

## Common Patterns

Parallel fan-out, then synthesis:

```yaml
nodes:
  - id: area.api
    mode: sequential
    providers: [builder]
  - id: area.ui
    mode: sequential
    providers: [builder]
  - id: summary
    mode: sequential
    needs: [area.api, area.ui]
    providers: [planner]
```

The `summary` prompt should consume `{{area.api.output}}` and
`{{area.ui.output}}`.

Parallel fail-safety:

```yaml
nodes:
  - id: review.parallel
    mode: parallel
    providers: [auditor-a, auditor-b, auditor-c]
    failure_threshold: 1
    continue_on_failure: true
```

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
commands run, and a link-only Generated Files section when files changed.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Inspect correctness, regressions, missing validation, and whether the executor
returned a complete candidate.
<!-- /crewplane:reviewer -->
```

Use this when the same node should iterate until reviewers approve or configured
depth is exhausted. Reviewers run against the current canonical executor
candidate; later audit rounds start fresh with respect to unresolved review
state.

## Output Handoff Conventions

Ask executor nodes that change files to include a `Generated Files` section with
links or paths only. Keep generated file contents in the workspace rather than
copying them into workflow results: `## Generated Files` followed by one path or
link per line.

For large non-code artifacts, prefer the same pattern: write the artifact to a
stable path, return a concise summary plus link-only references, and make the
downstream prompt name exactly which paths may be opened. Do not copy large file
contents through multiple node outputs.

When multiple providers produce outputs or findings, expect consolidated results
to preserve meaningful provider order where the runtime supports it. Design
downstream prompts to consume the consolidated artifact, not node-local stage
filenames.

## Validation Checklist

Before finishing, verify:

- Frontmatter is valid YAML and contains the required schema, name, and nodes.
- Node IDs are unique, lower-case, stable, and not reserved names.
- Every non-input node has exactly one root `## <node-id>` section.
- Input nodes have `source` and no Markdown body section.
- Every `needs` entry points to an existing node or composed imported node.
- Every `{{node.output}}` or `{{node.findings}}` reference points upstream.
- Every `{{node.findings}}` reference targets a `findings: true` node.
- Provider aliases exist in the target project configuration.
- Parallel node providers all use the executor role.
- Parallel `failure_threshold`, when present, is non-negative and less than the
  provider count.
- Sequential `depth` and `audit_rounds`, when present, are positive; `audit_rounds`
  is used only on multi-provider review loops and respects the target project's
  max audit-round setting.
- Review-loop provider roles are contiguous: executor providers first, reviewer
  providers second.
- Role markers are standalone, balanced, non-nested, and allowed for the node
  mode.
- Token-budget thresholds are positive when set, and fail thresholds are greater
  than or equal to warn thresholds when both are non-null.
- Large upstream artifacts are compressed through findings, extraction nodes, or
  file-backed manifests instead of repeated full-output injection.
- Downstream prompts that read files name exact paths and scope, and avoid broad
  re-inspection unless the node is explicitly an audit node.
- File and environment templates resolve in the intended execution environment.
- Generated file handoffs, when requested, are link-only.
- The workflow passes the target project's validator or dry-run command.

## Quality Bar

Prefer small, composable workflows over large all-purpose files. Make dataflow
obvious from `needs` and artifact references. Keep prompts precise enough that a
new provider invocation can perform the task with only the workflow text and the
explicit artifacts injected into it.
