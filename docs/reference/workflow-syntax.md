# Workflow Syntax Reference

Markdown workflows have two parts: YAML frontmatter that declares the DAG, and
Markdown sections that provide the prompts for non-input nodes.

```yaml
---
schema_version: "1.0"
name: "Example"
description: "Optional description"
nodes:
  - id: inspect
    mode: parallel
    providers: ["claude"]
---

## inspect
Inspect the project.
```

## Frontmatter Fields

| Field | Description |
| --- | --- |
| `schema_version` | Workflow schema version. Must match `SCHEMA_VERSION`. |
| `name` | Workflow name. |
| `description` | Optional workflow description. |
| `inputs` | Mapping of declared workflow input names to local input node IDs. |
| `imports` | Markdown workflow imports. |
| `worktrees` | Experimental logical workspace declarations. |
| `nodes` | Workflow node list. |

## Imports

| Field | Description |
| --- | --- |
| `path` | Markdown workflow path. |
| `as` | Import alias namespace. Must match `[a-z0-9._-]+`. |
| `with` | String parameter bindings for `{{param:key}}`. |
| `inputs` | Bind child workflow input names to local node IDs. |

Import paths resolve relative to the workflow file that declares them. Imports
are Markdown-only, alias-namespaced, cycle-checked, and bounded to the project
root. Duplicate aliases fail. Unused `with` parameters fail so misspelled
parameter names do not silently disappear.
`inputs` keys and bound node IDs must be non-empty strings after trimming.

## Experimental Worktrees

Workflow-level `worktrees` are valid only when
`settings.workspace.enabled: true`. They declare workflow-local logical source
lines and disposable workspaces; project config does not define default
worktree selectors.

| Field | Description |
| --- | --- |
| `kind` | `worktree` or `snapshot`. |
| `setup_profile` | Optional setup profile name. Only valid for `worktree`. |
| `create_branch` | Optional branch export for `worktree`. Defaults to `false`. |
| `branch_name` | Optional branch name. Requires `create_branch: true`. |

Worktree names must match `[a-z0-9._-]+`; `none` is reserved.

Kinds:

- `worktree`: mutable Git-backed source line that can emit
  `workspace-state*.json`, `workspace-bundles/*.bundle`, and optional local
  branch export.
- `snapshot`: writable disposable scratch space. It never emits source lineage
  or a branch.

Node selection:

- `worktree: <name>` selects a declared logical worktree.
- `worktree: none` opts out and uses the project root.
- If exactly one worktree is declared, non-input nodes without an explicit
  selector inherit it.
- If multiple worktrees are declared, provider nodes must select one explicitly
  or set `worktree: none`.

Same-name `kind: worktree` writers must be ordered by the DAG. Different
logical worktree names are independent source lines and are not merged
implicitly. `setup_profile`, `create_branch`, and `branch_name` are valid only
for `kind: worktree`; `branch_name` requires `create_branch: true`.

## Nodes

| Field | Description |
| --- | --- |
| `id` | Node ID. |
| `mode` | `parallel`, `sequential`, or `input`. |
| `providers` | Provider shorthand strings or provider objects. |
| `needs` | Upstream node IDs. |
| `continue_on_failure` | Treat selected parallel or review-loop failure policies as successful node completion. |
| `findings` | Write a findings artifact for this node. Defaults to `false`. |
| `source` | Input node file source. Only valid for `mode: input`. |
| `depth` | Positive sequential execution depth. Defaults to `1`. |
| `audit_rounds` | Positive sequential review-loop audit round count. Defaults to `1` when reviewers are present. |
| `review_starts_with` | `executor` or `reviewer` for sequential executor/reviewer review loops. Defaults to `executor`. |
| `failure_threshold` | Parallel-node failure threshold. Must be less than provider count. |
| `token_budget` | Node token budget override. |
| `worktree` | Experimental node worktree selector. Not valid for input nodes. |

Node IDs must match `[a-z0-9._-]+`, cannot be `.` or `..`, and cannot use the
reserved run-root names `logs`, `manifests`, or `workspace-exports`.

## Node Modes

`mode` selects the execution pattern inside a node:

| Mode | Providers | Mode-specific controls | Result selection |
| --- | --- | --- | --- |
| `input` | None. | `source` only. | The referenced file is copied as the node result. |
| `parallel` | One or more executors. Reviewers are rejected. | `failure_threshold`, `continue_on_failure`. | Latest artifact for each provider task is aggregated. |
| `sequential` with one provider | Exactly one executor. | `depth`. | Latest executor round is selected. |
| `sequential` with multiple providers | One or more executors followed by one or more reviewers. | `depth`, `audit_rounds`, `review_starts_with`, `continue_on_failure`. | Runtime review-loop status selects canonical executor and reviewer artifacts. |

Provider nodes can also use `findings`, `token_budget`, and `worktree` where
the field is otherwise valid.

Parallel nodes render one executor prompt and send it to each provider
concurrently. `settings.max_parallel_invocations` can cap provider calls inside
the node. `failure_threshold` defaults to `0`, so any provider failure fails the
node unless a threshold or `continue_on_failure` allows completion.

Sequential single-provider nodes do not run review loops. `depth` is the total
number of executor rounds, and `audit_rounds` is invalid.

Sequential multi-provider nodes run review loops. Providers must start with a
contiguous executor segment and end with a contiguous reviewer segment. In each
review round, reviewers receive the same reviewer prompt and current executor
output. With one reviewer, that reviewer must approve. With multiple reviewers,
all reviewers must approve.

`review_starts_with` controls only the first phase inside a sequential review
loop. It does not change `mode`, provider roles, or provider declaration order.
Omit it or set `executor` for the usual executor-candidate-then-reviewer flow.
Set `reviewer` to run a round-0 reviewer pass against existing review context
before the local-round-1 executor candidate. Reviewer-first nodes still require
both executor and reviewer providers, still finalize through the canonical
executor output, and do not add a downstream artifact protocol.

For review loops, `depth` is remediation depth inside each audit round. A
`depth` of `1` allows one fix attempt after the initial reviewed candidate.
`audit_rounds` controls fresh audit passes and must not exceed
`settings.max_audit_rounds`.

Executor-first review-loop order is:

```text
for audit_round in 1..audit_rounds:
  for local_round in 1..depth+1:
    executor candidate exists or is remediated
    reviewer providers review the current candidate
    stop if all reviewers approve
```

Local round 1 reviews the initial candidate. Later local rounds are remediation
attempts. If a later audit round has a valid output from the prior audit round,
it starts by re-reviewing that output as local round 1; otherwise it invokes the
executor for local round 1. A clean local-round-1 approval stops the whole loop.

Reviewer-first audit round 1 starts with a round-0 reviewer pass. If reviewers
approve, the local-round-1 executor still runs with preservation guidance so the
node produces a canonical same-node executor output. If reviewers report major
or minor issues, that feedback becomes the local-round-1 executor handoff.
`depth` still counts remediation attempts after the local-round-1 executor
candidate; the round-0 review does not consume depth.

## Provider Objects

```yaml
providers:
  - provider: codex
    model: gpt-5.5
    role: executor
```

Provider object fields:

- `provider`
- `model`
- `role`

Roles are `executor` and `reviewer`. Parallel nodes do not allow reviewers.
Sequential single-provider nodes must use one executor provider and cannot set
`audit_rounds`; `depth` is the total number of executor rounds. Sequential
multi-provider review loops must start with a contiguous executor segment and
end with a contiguous reviewer segment. Use `review_starts_with`, not provider
reordering, when reviewers should run before the first executor candidate.

Provider shorthand strings are executor providers. Use provider objects when a
provider needs a `model` override or `role: reviewer`.

## Input Nodes

Input nodes load a file without invoking a provider:

```yaml
nodes:
  - id: standards.file
    mode: input
    source: "{{file:docs/standards.md}}"
```

Rules:

- `source` must be exactly one raw `{{file:...}}` template.
- No Markdown body section is allowed.
- No `providers`, `needs`, `findings`, `depth`, `audit_rounds`,
  `review_starts_with`,
  `failure_threshold`, `continue_on_failure`, `token_budget`, or `worktree`
  selector is allowed.

## Prompt Sections

Every non-input node requires one `## <node-id>` section.

Unmarked Markdown is shared prompt content. Authored role markers are only:

```markdown
<!-- crewplane:executor -->
Executor-only prompt.
<!-- /crewplane:executor -->

<!-- crewplane:reviewer -->
Reviewer-only prompt.
<!-- /crewplane:reviewer -->
```

There is no authored `shared` marker.

Role markers must be standalone root-level HTML comments. Markers inside code
fences, blockquotes, or lists are treated as literal prompt text.

Executors receive shared content plus executor segments. Reviewers receive
shared content plus reviewer segments. In review loops, Crewplane also wraps
reviewer prompts with reviewer-only instructions, current executor output,
previous unresolved feedback when present, and the structured review contract.
For `review_starts_with: reviewer`, the round-0 reviewer prompt uses the same
shared plus reviewer content as existing review context before any same-node
executor candidate exists.

## Templates

Runtime template forms:

- `{{file:path}}`
- `{{env:KEY}}`
- `{{var:KEY}}`
- `{{node.output}}`
- `{{node.findings}}`
- `{{node.output_path}}`
- `{{node.findings_path}}`
- `{{node.output_size}}`
- `{{node.findings_size}}`
- `{{node.output_sha256}}`
- `{{node.findings_sha256}}`

`{{param:key}}` is composition-time only. Bound parameters are substituted
during Markdown workflow composition; unbound parameters are rewritten to
`{{var:key}}` for runtime variable resolution.

Relative `{{file:path}}` paths resolve from the project root, including when
the token is authored in an imported Markdown workflow. Imported workflow source
paths remain provenance metadata for diagnostics and audit. All resolved paths
are bounded to the project root unless explicitly allowlisted with
`settings.integrations.artifacts.options.allowed_template_paths`.

Node artifact references are valid only for upstream dependencies. Findings
references require the upstream node to declare `findings: true`.

`needs` orders nodes but does not automatically decide what reviewers inspect.
For reviewer-first review/fix nodes, put the review context in the prompt with
explicit references such as multiple upstream `{{node.output}}` or
`{{node.findings}}` values, metadata references like `{{node.output_sha256}}`,
or `{{file:path}}`. A standalone project-root reviewer-first node can review
visible project files with `{{file:...}}`; with Experimental managed
workspaces, reviewer-first file references read the compiled Git source state
selected by workspace policy rather than uncommitted manual edits.

## Token Budget Override

```yaml
token_budget:
  warn_threshold_chars: 100000
  fail_threshold_chars: 150000
```

`fail_threshold_chars` must be greater than or equal to `warn_threshold_chars`
when both are set.
