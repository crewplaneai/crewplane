# Workflows

The primary authored workflow format is Markdown: YAML frontmatter plus one
Markdown section per non-input node. YAML workflow files can be loaded directly,
but imports and composition are Markdown-only.

```yaml
---
schema_version: "1.0"
name: "Review"
description: "Review the repository"
nodes:
  - id: review.context
    mode: parallel
    providers: ["claude", "codex"]
---

## review.context
Review the current repository and report high-risk issues.
```

## Frontmatter

Frontmatter declares workflow metadata, optional inputs and imports, optional
Experimental worktrees, and executable nodes. The generated templates use the
current schema version from `src/crewplane/version.py`.

Node IDs use lower-case letters, digits, `.`, `_`, and `-`. They cannot be `.`,
`..`, `logs`, `manifests`, or `workspace-exports`. A non-input node must have
exactly one `## <node-id>` Markdown section. An input node has no authored body
section and uses `source` instead.

## Dependencies

Use `needs` to declare upstream dependencies:

```yaml
nodes:
  - id: inspect
    mode: parallel
    providers: ["claude"]
  - id: summarize
    mode: sequential
    providers: ["codex"]
    needs: ["inspect"]
```

Downstream prompts can reference upstream artifacts, for example
`{{inspect.output}}` or `{{inspect.findings}}`.

## Node Modes

Every workflow node has a `mode`:

| Mode | Meaning |
| --- | --- |
| `input` | Load one file artifact without invoking a provider. |
| `parallel` | Send the same executor prompt to one or more providers concurrently and aggregate their outputs. |
| `sequential` | Run one executor in order, or run an executor/reviewer review loop when multiple providers are configured. |

`mode: parallel` is provider fanout inside one node. It is different from DAG
concurrency, where independent nodes can run at the same time after their
dependencies are satisfied.

`mode: sequential` has two shapes. With one provider, it is a plain executor
node; `depth` is the total number of executor rounds. With multiple providers,
it is a review loop; providers must be declared as executor providers followed
by reviewer providers.

## Providers

Providers can be shorthand strings or objects:

```yaml
providers:
  - claude
  - provider: codex
    model: gpt-5.4
    role: reviewer
```

Roles are `executor` and `reviewer`. Parallel nodes only allow executor roles.
Sequential review loops use executor providers followed by reviewer providers.
Reviewers approve with `NO_FINDINGS` or `NITS_ONLY`; all reviewers must approve
for consensus.

For examples and configuration guidance, see
[Node modes and provider roles](../guides/node-modes.md).

## Templates

Supported runtime template forms are:

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

See the [workflow syntax reference](../reference/workflow-syntax.md) for the
complete authoring contract.
