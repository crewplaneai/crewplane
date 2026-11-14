---
name: create-workflows
description: >-
  Create or revise Crewplane .task.md workflow graphs. Use when authoring,
  simplifying, composing, or validating a workflow for a requested task.
---

# Create Workflows

Create self-contained `.task.md` workflows that coordinate independent AI
provider invocations through explicit files and artifacts. Treat workflow files
as infrastructure: declarative, reviewable, deterministic to validate, and
portable across projects that use the same workflow format.

## Authoring procedure

1. Inspect target workflows, supported schema, configured agent IDs and actual
   invoker capabilities, and relevant project instructions. Preserve the user's
   providers and scope. Do not require Crewplane implementation sources, external
   skills, or live research just to author a workflow.
2. Define the result, audience, evidence/input sources, authorized mutations, and
   acceptance evidence. Report genuinely missing information or capability; do
   not substitute a different task. Authoring does not authorize provider setup,
   config changes, mutating execution, branch export, or publication.
3. Choose the fewest nodes with meaningful ownership, capability, concurrency,
   or verification boundaries. One node may perform tightly coupled steps.
   Independent read-only branches can fan out. Serialize overlapping project-root
   writes; separate investigation from mutation when evidence must come first.
4. Give every edge an explicit [information contract](#handoff-selection).
   `needs` orders execution; it never injects content. A new invocation must be
   able to work from its prompt and the artifacts it can actually read.
5. Select only mechanisms the task needs. Define outcome-specific failure and
   where unresolved evidence, incomplete deliverables, or review disagreement
   will be reported. Findings, budgets, reviews, workspaces, and repetition are
   optional, not completeness checkboxes.
6. [Validate within scope](#validation-and-handoff), then report the evidence
   actually obtained. A compiled graph, a ready provider, and a verified task
   result are three different claims.

Put stable task, constraints, and output requirements before injected context.
Name exact read scope and distinguish evidence from instructions. Avoid asking
successive nodes to re-inspect everything unless that is their assigned job.

## Task patterns, not mandatory stages

| Task | Useful boundaries when distinct | Handoff | Completion evidence |
| --- | --- | --- | --- |
| Research, architecture, design, planning | Investigate → synthesize/design → focused evidence or decision review | Complete evidence-bearing result, including rationale and constraints | Sources, scope, material alternatives, decisions, unresolved questions; usable without hidden conversation context |
| Audit or code review | Establish scope → inspect → report; remediation only if requested | Complete audit report; findings only for an intentional issue consumer | Coverage, evidence, severity/impact, actionable locations, limitations; no unrequested edits |
| Debugging | Reproduce → test a causal hypothesis → minimal fix → verify | Sufficient self-contained findings, or complete investigation/logs | Observed/expected behavior, reproduction, confirmed cause versus hypothesis, regression evidence, exact checks |
| Implementation/refactor | Plan when needed → change → targeted verification/review | Plan plus explicit source-line or project-root mutation contract | Required behavior, changed files, affected checks, remaining risks |
| Documents/content/creative work | Audience/context → produce → reader or visual review → revise | Full document/native artifact and retained links | Requested format, completeness, readability/rendering or task-specific quality checks |
| Data/file transformation | Inspect inputs/assumptions → transform → inspect native result | Durable file and verification record | Data/formula/schema correctness, provenance, opening/recalculation/rendering as relevant |
| Operational work | Establish authorized state/target → bounded action → verify | Operation record and necessary evidence | Actual resulting state, unresolved effects, appropriate rollback/recovery; a DAG grants no authority |

Do not add a separate planning, review, validation, or token-reporting node without
an actual task boundary.

## Minimal complete workflow

This example uses schema `1.0`. `alpha` denotes an agent ID that must already be
configured in the target project, not a built-in provider or a setup instruction.
Use the user's configured ID and supported schema; an unknown placeholder is not
a validated workflow.

```markdown
---
schema_version: "1.0"
name: Plain-language rewrite
nodes:
  - id: rewrite
    mode: parallel
    providers: [alpha]
---

## rewrite

Rewrite the following notice in plain English for customers. Preserve its facts
and return only the revised notice, in one sentence:

"Service will be unavailable from 02:00 to 03:00 UTC on Sunday for maintenance."
```

## Ordinary syntax and mode selection

Use YAML frontmatter with required `schema_version`, `name`, and `nodes`;
`description` is optional. Schema and package versions are different contracts.
Each node has a stable, unique `id` and a `mode`. IDs match lower-case
`[a-z0-9._-]+`, excluding `.`, `..`, `logs`, `manifests`, `preflight`, and
`workspace-exports`. Keywords and artifact names are case-sensitive.

| Mode value | Providers and Markdown section | Choose it for |
| --- | --- | --- |
| `parallel` | One or more executors; exactly one root `## <id>` section with an executable prompt | One shot, or the **same prompt** independently sent to multiple executors; not per-item mapping |
| `sequential`, one provider | One executor; executable section | Ordered repeated passes. `depth` is total invocations, default 1; no automatic review or prior-output injection |
| `sequential`, multiple providers | Executors first, then reviewers; executable prompts for each scheduled role | A [review loop](references/advanced-workflows.md#review-and-failure-policy), where `depth` instead bounds remediation |
| `input` | No provider and **no Markdown section** | A [raw-file/public input boundary](references/advanced-workflows.md#composition-and-inputs); a remaining materialized node needs `source` |
| `foreach` | No provider or executable prompt; exactly one empty/HTML-comment-only `## <id>` section | [Bounded iteration](references/advanced-workflows.md#bounded-foreach) of a fixed child graph over objects |

Independent ready DAG nodes can run concurrently regardless of their individual
mode. `needs` lists upstream IDs, including qualified imported IDs. Provider
nodes require `providers`; shorthand such as `providers: [alpha]` always means
executor. An agent's name does not assign its role.

Provider objects accept `provider`, optional `model`, optional `reasoning`, and
`role` (`executor` by default). Reviewer objects are valid only in multi-provider
sequential review loops, never parallel or reviewer-only nodes. `reasoning` is an
opaque invoker-specific token, not a portable effort scale. The built-in CLI
supports it for Codex/Claude; mock acceptance does not prove live support, and
other invokers require their actual capability contract. Omit unneeded overrides.

Use advanced fields only after reading the corresponding contract:

- `imports` and public `inputs`: [composition and inputs](references/advanced-workflows.md#composition-and-inputs).
- `workflow`, `item_input`, `items`, `max_items`, group `inputs`/`with`: [bounded foreach](references/advanced-workflows.md#bounded-foreach).
- Top-level `worktrees` and provider-node `worktree`: [workspace selection and source lineage](references/advanced-workflows.md#workspaces).
- `depth`, `audit_rounds`, `review_starts_with`, `continue_on_failure`, `failure_threshold`: [review and failure policy](references/advanced-workflows.md#review-and-failure-policy).
- `findings` and `token_budget`: [optional guards and extraction](references/advanced-workflows.md#optional-guards).
- Root `repeat_force_run_count` or fresh execution: [run controls](references/advanced-workflows.md#run-controls).

## Handoff selection

Choose the smallest handoff that preserves correctness, not the smallest summary.

| Consumer needs | Use | Guardrail |
| --- | --- | --- |
| Complete reasoning, evidence, design, or audit context | `{{producer.output_path}}`; explicitly instruct the consumer to open the complete result | Only when its provider can open that file on its execution host. A text-only provider needs deliberately bounded complete inline content or an explicit capability blocker |
| A deliberately small result to transform | `{{producer.output}}` | Intentional inline content, not a universal fallback for large reports |
| A concise, potentially lossy issue/action packet | Producer `findings: true`, then `{{producer.findings}}` or `{{producer.findings_path}}` | Preserve necessary reproduction, evidence, and constraints; use the [extraction contract](references/advanced-workflows.md#optional-guards), not invented summaries |
| A generated work-item collection | One executor with findings containing only a JSON object array; a bounded foreach consumer | [Typed collection rules](references/advanced-workflows.md#bounded-foreach), not bullets/prose; `[]` is meaningful |
| A binary/native/generated file | Explicit Generated Files claims and verified retained result links | Original workspace paths or successful provider output do not prove capture succeeded |
| Size or identity of an artifact | Add `*_size` or `*_sha256` only for a named consumer decision | Byte size for a reading/limit decision; SHA-256 for identity, integrity, copy comparison, or an explicit audit value. No routine metadata boilerplate |

Exactly eight artifact names are supported: `output`, `findings`, `output_path`,
`findings_path`, `output_size`, `findings_size`, `output_sha256`, and
`findings_sha256`. References must target transitive ancestors; **all**
findings-family references require that ancestor's `findings: true`. Path tokens
inject paths, not contents. There is no `generated_files` artifact template,
item-field interpolation, or general workspace-path template.

A node result is a consolidated document, potentially with multiple provider/task
sections and captured-file links. Identify the relevant task/candidate when
necessary; consume canonical results, not guessed node-local stage filenames.
Across nodes, do not duplicate a report into an extra file just to hand it off.
Within a review loop, however, return the complete **current** candidate, or a
substantive current file-deliverable report with Generated Files links. A redirect
such as "see the previous round" is not a current candidate.

Other supported templates are `{{file:path}}`, `{{env:KEY}}`, `{{var:KEY}}`, and
composition-time `{{param:key}}`. Relative file paths are project-root-relative,
including in imports, and stay within existing path permissions. `project_name`
is the built-in runtime variable; bind arbitrary parameters explicitly through
[composition](references/advanced-workflows.md#composition-and-inputs). Check
file/environment availability on the intended host; do not let unresolved tokens
silently reach a provider.

## Files and mutation boundaries

Keep result text, captured generated-file copies, and mutable code lineage
separate: none implies either of the others. An artifact path is not a code merge.
For isolated edits or retained source lines, read [workspaces](references/advanced-workflows.md#workspaces).
Shared project-root writes need explicit non-overlap or DAG ordering.

Ask file-producing executors for a simple exact `## Generated Files` heading with
one concrete changed workspace-relative path or Markdown link per line, without
narrative on those lines. This is the reliable recommended convention, not a
claim that the parser accepts no other headings. For example, a response can end:

```markdown
## Generated Files
[Report](deliverables/report.pdf)
```

Project-root capture requires explicit claims and files verifiably changed in
that invocation. Crewplane retains eligible copies beneath
`.crewplane/execution-results/<run-key>/generated-files/<node>/<task>/` and adds
canonical links to the consolidated result. Capture is bounded and best-effort;
a valid provider response can still have a capture warning or missing file.

If a native deliverable is required, verify the retained link and usable contents
with its task-specific opening, rendering, recalculation, or schema checks.
Report missing/filtered capture as an incomplete deliverable, not success or a
summary substitute. Temporary workspace/cache paths are not durable handoffs.

## Validation and handoff

1. Inspect frontmatter, schema, IDs, section/mode rules, configured providers,
   roles, ancestor references, and file containment. Check composed inputs and
   prerequisites for each selected advanced mechanism. Do not validate an
   import-required child as if it were standalone; check its complete parent.
2. With existing tooling and target config, compile/preview without provider
   availability probes or task invocation:

   ```bash
   crewplane run --dry-run --tasks PATH
   # For an existing explicit config:
   crewplane run --dry-run --tasks PATH --config CONFIG
   ```

   Replace `PATH` and `CONFIG` with real paths. Dry-run writes no run artifacts;
   it does not prove readiness, dynamic collection values, or task success.
3. Use `crewplane validate PATH` (optionally `--config CONFIG`) only when provider
   readiness checking is in scope. It writes no run artifacts but can perform
   configured availability checks, including diagnostic inference. It is **not**
   the provider-free syntax-check alternative.
4. Fix authored errors and report missing tools, config, IDs, or capabilities
   precisely. Do not install dependencies, change setup, or execute the workflow
   as an unrequested workaround. If compilation is unavailable, label the work
   manually inspected, not validated by the CLI.
5. Hand off changed workflow paths, selected mechanisms and prerequisites, and
   unresolved gaps. Separate static/manual inspection, dry-run, readiness probes,
   and actual execution evidence. If execution was separately authorized, report
   the verified outcome through durable result/summary links, including missing
   files and unresolved review rather than implying approval or completion.
