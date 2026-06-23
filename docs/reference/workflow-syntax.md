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
| `failure_threshold` | Parallel-node failure threshold. Must be less than provider count. |
| `token_budget` | Node token budget override. |
| `worktree` | Experimental node worktree selector. Not valid for input nodes. |

Node IDs must match `[a-z0-9._-]+`, cannot be `.` or `..`, and cannot use the
reserved run-root names `logs`, `manifests`, or `workspace-exports`.

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
end with a contiguous reviewer segment. For review loops, `depth` is remediation
depth inside each audit round.

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

`{{file:path}}` paths in the entry workflow are resolved from the project root
when relative. In imported Markdown workflows, relative file paths resolve from
the imported workflow file's directory. All resolved paths are bounded to the
project root unless explicitly allowlisted with
`settings.integrations.artifacts.options.allowed_template_paths`.

Node artifact references are valid only for upstream dependencies. Findings
references require the upstream node to declare `findings: true`.

## Token Budget Override

```yaml
token_budget:
  warn_threshold_chars: 100000
  fail_threshold_chars: 150000
```

`fail_threshold_chars` must be greater than or equal to `warn_threshold_chars`
when both are set.
