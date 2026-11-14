# Advanced Workflow Contracts

Read only the section needed by the graph being authored. Start with the
[entrypoint](../SKILL.md) for task selection, ordinary syntax, canonical handoff
choices, Generated Files, and validation scope.

- [Composition and inputs](#composition-and-inputs): reusable modules and phase ordering.
- [Bounded foreach](#bounded-foreach): one fixed child graph per collection item.
- [Workspaces](#workspaces): disposable snapshots and mutable source lines.
- [Review and failure policy](#review-and-failure-policy): role prompts and bounded review.
- [Optional guards](#optional-guards): findings extraction and injected-value limits.
- [Run controls](#run-controls): verified reuse, fresh runs, and explicit repetition.

Examples use schema `1.0` and agent IDs `alpha` and `codex`.
These IDs must already exist in the target config with the capabilities their
prompts require; they are not provider setup or evidence of live readiness.
Validate complete pairs through their parent with the existing target config.

## Composition and inputs

Use imports for genuinely reusable graphs, not to add stages to a simple task.

- `imports[].path` and `as` are required. Imports are Markdown-only, resolve
  relative to the declaring workflow, and must stay inside project root.
  Relative `{{file:path}}` references remain **project-root-relative**, including
  inside imports. Do not weaken path guards.
- Aliases match `[a-z0-9._-]+` and are unique in their declaring file. Imported
  nodes are qualified as `alias.node`; nested imports form `outer.inner.node`.
  All files use the root's supported schema. Cycles, qualified-ID collisions,
  unknown dependencies, and non-ancestor artifact references fail validation.
- `with` maps string parameters for literal, one-pass `{{param:key}}`
  substitution in **prompt text only**, never IDs, providers, dependencies,
  sources, or other frontmatter. Unused bindings fail. Unbound parameters become
  `{{var:key}}` and must resolve at preflight; `project_name` is the built-in
  runtime variable, not a general source of arbitrary parameter values.
- Top-level `inputs` maps public names to local `mode: input` node IDs.
  `imports[].inputs` maps those public names to caller node IDs, not templates.
  Binding transfers artifact identity and rewrites consuming dependencies and
  recognized artifact references; it does not select a findings summary. The
  bound input is pruned, and its fallback source is not read. Unknown names,
  duplicate normalized keys, or binding the same child input twice are invalid.
- A required imported-module input may omit `source`; validate it through a binding
  parent, not standalone. An unbound input that remains materialized requires
  exactly one raw `{{file:path}}` source and no Markdown section. Input nodes
  have no providers, dependencies, findings, review/failure controls, budget,
  or worktree selector.
- `imports[].needs` is a list of caller-scoped node IDs, not public input names,
  artifact templates, or parameters. It adds prerequisites to **every imported
  provider-entry branch** after composition, preserving internal ordering.
  Omission or `[]` adds none. An alias alone never means completion of the whole
  import; name concrete nodes. Nonempty prerequisites with no provider entry
  are invalid. These edges retain ordinary unknown-target, cycle, and
  cross-worktree restrictions.

Keep the mechanisms distinct: `needs` orders all entries, `inputs` binds data
identity, and `with` substitutes prompt parameters. Ordering never injects
content or defers raw file reading. An unbound file input reads its supported
preflight/project-initial source, not a future file an upstream provider will
create. Use producer artifacts and bindings for generated data.

Prefer current-run artifacts. Historical `{{file:...}}` access is restricted to
supported `.crewplane/execution-results/<run-key>/` from terminal runs
(`succeeded`, `failed`, or `cancelled`), verified and materialized at preflight.
Running results, live stages, checkpoints, and other runtime-owned paths are not
an alternate input channel. Never edit runtime-owned files.

### Complete composition pair

Save these two files together. `reusable-brief.task.md` requires its public input:

```markdown
---
schema_version: "1.0"
name: Reusable Brief
inputs:
  brief: brief-input
nodes:
  - id: brief-input
    mode: input
  - id: summarize
    mode: parallel
    needs: [brief-input]
    providers: [alpha]
  - id: criteria
    mode: parallel
    providers: [alpha]
---

## summarize
Rewrite this deliberately short brief for {{param:audience}} without adding facts:
{{brief-input.output}}

## criteria
List three readability criteria for a brief addressed to {{param:audience}}.
Do not inspect or change project files.
```

`composition.task.md` binds the input and orders both imported entry branches:

```markdown
---
schema_version: "1.0"
name: Composed Brief
imports:
  - path: ./reusable-brief.task.md
    as: brief
    needs: [source]
    inputs:
      brief: source
    with:
      audience: new team members
nodes:
  - id: source
    mode: parallel
    providers: [alpha]
---

## source
Return only this short factual brief: Team notes are published each Friday.
New members should read the notes before Monday's discussion.
Do not inspect or change project files.
```

Here `brief.summarize` consumes the bound source output; `brief.criteria` is
ordered after `source` without receiving its contents. Neither parameter
substitution nor ordering creates an implicit data handoff.

## Bounded foreach

Use `mode: foreach` for bounded per-item work, not same-prompt provider voting.
The only group fields are `id`, `mode`, `needs`, `workflow`, `item_input`,
`items`, `max_items`, `inputs`, and `with`. The group has an empty or
HTML-comment-only `## <id>` section. Provider, executable prompt, review,
budget, findings, worktree, and failure fields belong on child provider nodes;
they are invalid on the group even when explicitly empty or false.

- `workflow` is one fixed project-contained Markdown path relative to the
  declaring workflow. `item_input` names a declared public child input whose
  input-node output receives **one JSON object**. At this baseline the item
  input still needs a raw-file `source` for child-template validation; unlike a
  pruned import input, the item slot remains in the template. Foreach binding
  supplies its value and does **not** read that fallback file.
- `items` is a literal array of JSON objects (including `[]`) or exactly one
  upstream `{{node.output}}` / `{{node.findings}}` reference. Paths, globs,
  commands, expressions, concatenated references, and metadata are not sources.
  Read a collection file through an ordinary input node.
- Literal arrays may omit `max_items`; a supplied cap must be at least their
  length. Dynamic references require a strict positive integer cap. Explicit
  null, boolean, string, fractional, zero, or negative caps are invalid.
- `.output` collections must come from input or foreach nodes. Provider
  collections require `.findings` from exactly one executor with `findings: true`.
  Inside the [findings markers](#findings), emit only a JSON object array, not
  prose, bullets, or a JSON fence. `[]` is a valid nonempty findings body.
  Runtime propagates the effective consumer cap into producer prompts (the
  smallest cap when several groups share a producer). Do not contradict it with
  prose limits; input/index sources do not propagate caps back to earlier work.
- Collection and shared-input sources must already be ancestors through `needs`.
  `inputs` binds other child public inputs to caller node IDs; `with` supplies
  shared prompt parameters. Input/parameter validation and fallback semantics
  remain those of composition. Do not also bind the item input through `inputs`.
- No nested foreach, including through child imports. Do not reference generated
  children from authored parent nodes or derive node IDs from item fields.
  Generated identities are ordinal; template-looking item strings remain data.
- Existing finite `settings.max_concurrent_nodes` and sufficient
  `settings.max_expanded_nodes` are prerequisites, not group fields or permission
  to change setup. Collections and result indexes are limited to 1 MiB canonical
  UTF-8 JSON and nesting depth 64. Objects require unique string keys and JSON
  values; non-finite numbers and invalid data fail. Keep items compact. Missing
  caps, malformed/oversized sources, and projected index overflow do not trigger
  truncation or automatic regeneration.

Every child template validates even for `[]`. An empty collection succeeds with
an empty index and no child invocation. A failed child prevents a successful
join and blocks dependents; independent branches may continue. There is no
partial-success switch.

The group's output is an **ordered JSON result index**, not concatenated prose.
Entries contain `index`, `item`, and `results`; terminal descriptors include
artifact paths and framework size/hash metadata. Open `{{group.output_path}}`,
parse the index, and resolve descriptor paths **relative to that index file's
directory** before opening selected terminal results. Do not echo or recompute
metadata without a task-specific reason.

### Complete parent/child pair

`topics.task.md`:

```markdown
---
schema_version: "1.0"
name: Compare Topics
nodes:
  - id: topics
    mode: foreach
    workflow: ./topic.task.md
    item_input: topic
    items:
      - {question: "Compare written notes with a live briefing"}
      - {question: "Compare weekly and monthly briefings"}
  - id: synthesize
    mode: parallel
    needs: [topics]
    providers: [alpha]
---

## topics
<!-- The child receives one topic object per instance. -->

## synthesize
Open {{topics.output_path}} and parse its ordered JSON index. For each entry,
read the terminal result descriptors in results. Resolve their artifact paths
relative to the index file's directory, then open the complete analysis results.
Synthesize the comparisons, preserving constraints and unresolved questions.
Do not modify project files.
```

`topic.task.md`:

```markdown
---
schema_version: "1.0"
name: Analyze One Topic
inputs:
  topic: topic-item
nodes:
  - id: topic-item
    mode: input
    source: "{{file:unused-topic.json}}"
  - id: analyze
    mode: parallel
    needs: [topic-item]
    providers: [alpha]
---

## analyze
Parse this JSON object and address its question:
{{topic-item.output}}
Return a self-contained comparison with assumptions, tradeoffs, and unresolved
questions. Distinguish reasoning from externally verified facts. Do not modify files.
```

The parent supplies every item, so `unused-topic.json` need not exist and is not
read. This source declaration satisfies template validation; omitting it fails
at this baseline. To run the child standalone instead, supply a real source
file. Do not create a future-file dependency or substitute fallback data for a
failed collection.

For a dynamic variant, add a preceding single-executor `questions` node with
`findings: true`, have it emit the JSON object array in its findings block, then
set the group's `needs: [questions]`, `items: "{{questions.findings}}"`, and
`max_items: 4`. Keep the child unchanged. A file-backed input or earlier foreach
index uses `.output` instead and still requires the cap. Dry-run cannot prove a
future producer will emit valid JSON or fit the bound.

Foreach does not itself isolate writes. Child [workspaces](#workspaces) are
separate per instance and start from captured project source; unselected child
providers still share project root. Avoid overlapping writes. Shared artifact
bindings do not transfer mutable caller checkouts, and explicit export branch
names must be unique across instances.

## Workspaces

Select a workspace only when the requested mutation or isolation requires it.

| Selection | Meaning | Boundary |
| --- | --- | --- |
| No selected worktree / `worktree: none` | Shared project root | Serialize overlapping writes; current uncommitted work is visible within task scope. |
| Declared `kind: snapshot` | Writable disposable captured-source workspace | Edits are discarded; retain deliverables as artifacts. Not a security sandbox or latest-upstream-source view. |
| Declared `kind: worktree` | Mutable Git-backed logical source line | Exactly one executor per provider node; DAG-order all same-line writers. Reviewers may be concurrent. |

Top-level `worktrees` maps logical names to declarations; provider-node
`worktree` selects a name. Names match `[a-z0-9._-]+`, with `none` reserved.
One declaration is inherited by provider nodes lacking a selector; with multiple
declarations, each provider node selects explicitly or uses `none`. Input and
foreach nodes cannot select worktrees. Old `workspace` frontmatter is invalid.

Require existing `settings.workspace.enabled: true`, supported clean Git source,
and a compatible invoker. Native Windows managed workspaces are unsupported.
Tracked modifications fail clean-start checks; excluded untracked files are not
imported. Do not discard work or change config to pass these prerequisites.
Review current uncommitted changes in a compatible project-root design instead.

Same-name writers preserve their supported source lineage; different names are
independent alternatives. Direct dependencies between different mutable lines
are rejected. `needs`, artifact paths, and Generated Files capture do not merge
code. A project-root or snapshot reporting node may compare artifact reports,
but does not automatically contain upstream edits. Design required integration
explicitly within supported boundaries, rather than promising an implicit merge.

File templates in managed workspaces resolve against recorded initial/inherited
source or current review candidate as supported, not unrestricted latest files
in the original root. Executors edit and report validation; they must not commit,
switch branches, or change Git policy files such as `.gitignore` and
`.gitattributes`. Source HEAD/policy violations fail capture. Source isolation
does not isolate external side effects. Temporary workspace/cache paths are not
durable handoffs; result text, captured files, and code lineage remain distinct.

`setup_profile` selects an already configured worktree setup and may rerun after
resets/retries. `create_branch: true` and optional `branch_name` request a local
export only for `kind: worktree`; `branch_name` requires `create_branch: true`.
Use only for an explicitly requested outcome. Exports do not overwrite differing
branches, may be satisfied from verified resumed history without rerunning
providers, and never imply push, PR, merge, or checkout. Snapshots accept none of
these worktree-only options.

### Complete workspace selection example

Only use in a compatible clean disposable Git context with the prerequisites
above. This example requests no branch export:

```markdown
---
schema_version: "1.0"
name: Draft and Refine on One Source Line
worktrees:
  draft:
    kind: worktree
  report:
    kind: snapshot
nodes:
  - id: write
    mode: parallel
    worktree: draft
    providers: [alpha]
  - id: refine
    mode: parallel
    worktree: draft
    needs: [write]
    providers: [alpha]
  - id: report
    mode: parallel
    worktree: report
    needs: [refine]
    providers: [alpha]
---

## write
Create briefing.md containing a short welcome for new team members. Edit and
report only; do not commit, switch branches, or change Git policy files.
Return a substantive file-deliverable report and an exact ## Generated Files
section with briefing.md on its own line.

## refine
Open the complete report at {{write.output_path}}. Refine briefing.md on this
inherited source line for clarity without inventing facts. Do not commit, switch
branches, or change Git policy files. Return the current deliverable report and
an exact ## Generated Files section with briefing.md on its own line if changed.

## report
Open {{refine.output_path}} and follow its retained briefing.md link. Verify the
retained file is readable and report the result, or report missing capture as an
incomplete deliverable. Do not assume this snapshot contains upstream edits.
Do not change files.
```

## Review and failure policy

Use multi-provider sequential mode for an actual executor/reviewer loop.
A standalone audit is an executor task, not a reviewer-only node. Declare all
executors first, then all reviewers; parallel nodes allow executors only.

- Executors run in declaration order and produce the canonical candidate set.
  Reviewers run concurrently; every declared reviewer must approve. A failed
  reviewer never counts as approval.
- `review_starts_with: executor` is the default. `reviewer` requests an initial
  round-zero review with **real explicit file/artifact context**, before a
  same-node candidate exists. It does not consume depth or remove the executor:
  even initial approval still requires canonical executor output.
- Review-loop `depth` is positive remediation attempts after the initial
  candidate per audit (default 1). `depth: 2` permits up to three candidate/review
  cycles per audit. This differs from single-provider sequential `depth`, which
  counts total invocations and injects no prior output automatically.
- `audit_rounds` is positive, defaults to 1, and cannot exceed existing
  `settings.max_audit_rounds`. A clean first-pass approval ends later audits;
  a fresh audit begins from the latest valid candidate without carrying prior
  unresolved review state.
- Repeated unchanged remediation may stop early for `no_progress`. Increasing
  depth guarantees neither more work nor approval. Missing canonical output
  always fails.
- The consolidated result selects canonical executor artifacts and the latest
  selected reviewer artifacts, not every candidate as equally final. Within
  each executor response return the complete current candidate or substantive
  current file-deliverable report, never only a redirect to a previous round.

### Role markers

Runtime owns current-candidate injection, unresolved feedback, reviewer behavior,
and verdict formatting. Reviewer prompts specify task-specific criteria and
evidence, not a copied framework contract. Shared text appears in every role;
executor/reviewer blocks add only their role's instructions.

Complete `review.task.md`:

```markdown
---
schema_version: "1.0"
name: Review a Short Brief
nodes:
  - id: brief
    mode: sequential
    depth: 2
    audit_rounds: 1
    providers:
      - provider: alpha
        role: executor
      - provider: codex
        role: reviewer
---

## brief
The audience is new team members. Facts: notes are published Friday; discussion
is Monday. The deliverable is a two-sentence welcome, not a project file edit.

<!-- crewplane:executor -->
Write or revise the welcome. Return the complete current text each time.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Check factual fidelity, the two-sentence requirement, and clarity for newcomers.
Cite specific wording for any substantive concern; ignore optional stylistic polish.
<!-- /crewplane:reviewer -->
```

Markers must be standalone root-level HTML comments, balanced, non-nested, and
nonempty. Only `executor` and `reviewer` exist; there is no `shared` marker.
Marker-like text in fences, lists, blockquotes, or paragraphs is literal.
Parallel and single-provider sequential nodes permit shared/executor segments;
only multi-provider sequential review loops permit reviewer segments. Every
scheduled role must render a nonempty prompt.

### Failure is not approval

`continue_on_failure: true` permits supported continuation, not universal
exception recovery or reviewer approval. Ordinary consensus exhaustion can
follow existing config policy (currently default continue), even when the node
flag is false/omitted. No-progress continuation specifically requires the node
flag. For a hard approval gate, inspect the existing exhaustion policy and
report a config prerequisite if workflow-only changes cannot assure it;
`continue_on_failure: false` alone is not a hard gate.

Parallel `failure_threshold` is a nonnegative count below provider count,
defaulting to zero. Supported invocation failures within it are accepted; above
it, `continue_on_failure: true` can permit partial completion with failure
artifacts. Unexpected defects are not covered. Failed lineage-producing
worktree executors remain fatal regardless of these knobs. Reviewer-invocation
continuation likewise neither approves a candidate nor generally recovers
executor failures.

Report unresolved review as unresolved. Point completion reports to durable
runtime summary/result links and unresolved-review diagnostics, not invented
per-node token totals or duplicate runtime telemetry.

## Optional guards

### Findings

Enable `findings: true` only for an actual concise-packet or collection consumer.
Eligible executor output must contain exactly one nonempty block with these
standalone, balanced, non-nested markers:

```text
<!-- findings -->
The intentionally selected consumer packet goes here.
<!-- /findings -->
```

Do not silently invent findings when extraction fails, or discard evidence and
constraints needed by the consumer. Review-loop reviewer outputs do not
participate in findings extraction. Supported synthetic parallel-failure
artifacts are the exception: runtime supplies failure findings for those failed
executor invocations. Multiple eligible outputs consolidate in provider order;
that does not make aggregate findings a valid foreach collection.

### Injected-value budgets

Add node `token_budget` only for concrete injected-value risk or a requested cap.
Omission inherits config; it does not disable protection. Each omitted key
inherits, and each explicit `null` disables only that threshold. Non-null
thresholds are positive; the **merged** fail threshold must be at least the warn
threshold when both exist. Current defaults are a 50,000-character warning and
no fail threshold. A fail-only override below that inherited warning is invalid.

For a specifically requested 40,000-character failure cap, a valid node fragment
sets both keys rather than accidentally inheriting the higher warning:

```yaml
token_budget:
  warn_threshold_chars: 20000
  fail_threshold_chars: 40000
```

Limits apply independently to each injected artifact, runtime-resolved file,
and prior review candidate. They measure characters, not aggregate prompt size,
model tokens, money, or later file reads through a path. Warnings preserve the
full value; failures stop before invocation. Prefer redesigning an oversized
handoff to raising limits routinely. Never silently truncate canonical data.

## Run controls

Ordinary runs can skip or resume verified previous work. Use `--force` only when
fresh execution/inspection is intended; do not invent `--resume` or edit
checkpoints. Writing a workflow is not authorization to execute it.

`repeat_force_run_count` is a rare, explicitly requested root-level strict
positive integer. It forces that many whole fresh runs, **including at 1**.
It is not foreach, sequential depth, review remediation, or an invocation retry.
Imported/child values do not multiply execution. Config and workflow reload
between passes; earlier project-root mutations persist.

Repetition requires workspace isolation disabled and no worktree declarations
anywhere, including unused/imported ones. Do not change config to manufacture
compatibility. Require authority and a stopping policy for repeated
non-idempotent effects. Dry-run previews only the first pass, not later modified
inputs. Keep provider-free dry-run, readiness probes, and actual execution
claims separate as described in the [entrypoint](../SKILL.md).
