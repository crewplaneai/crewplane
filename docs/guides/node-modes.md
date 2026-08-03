# Node Modes And Provider Roles

The workflow authoring guide starts at the DAG level: `needs` decides which
nodes wait for upstream artifacts. This guide zooms in on **one node**.

Inside a node, `mode` chooses the invocation shape, `providers` selects the
configured agents, `model` can override an agent's default model for that
invocation, `reasoning` can request provider-native reasoning effort, and
`role` decides whether a provider executes the work or reviews a candidate.

## Choosing A Mode

Most users start with `parallel`. It is the simplest provider invocation shape,
even when there is only one provider.

| Mode | Use when | Provider shape |
| --- | --- | --- |
| `input` | A workflow needs to expose a file as an artifact without invoking a provider. | No providers. |
| `parallel` | Several providers should answer the same prompt independently. | One or more executor providers. |
| `sequential` | One provider should run in order, or executor output should be reviewed and remediated. | One executor, or executor segment followed by reviewer segment. |

Independent DAG nodes can also run concurrently when their dependencies are
satisfied. That is separate from `mode: parallel`, which is provider fanout
inside one node.

## Provider Entries, Models, Reasoning, And Roles

Every non-input node lists one or more configured agents in `providers`. A
string uses that agent's default model, adds no Crewplane reasoning override,
and assigns the executor role:

```yaml
providers: [codex]
```

### Use Provider Objects

Use object form when one node invocation needs a model or reasoning override,
or an explicit role:

```yaml
providers:
  - provider: codex
    model: gpt-5.5
    reasoning: high
    role: executor
  - provider: claude
    role: reviewer
```

### Choose Reasoning Effort

In the example above, `reasoning: high` asks Codex to use high reasoning effort
for that invocation. Claude has no workflow override and uses its normal
configuration.

The value is provider-native: Codex and Claude define which values their models
support. Crewplane currently supports this field for the built-in `cli` invoker
when `provider_kind` is `codex` or `claude`.

When `reasoning` is omitted or set to `null`, Crewplane sends no reasoning
override and the provider uses its normal configuration. Do not also configure
the provider's native reasoning option in `cli_cmd` or `extra_args`.

See [Provider setup](../getting-started/provider-setup.md#choose-reasoning) for
the native Codex and Claude transports, and the
[workflow syntax reference](../reference/workflow-syntax.md#provider-objects)
for the complete validation contract.

### Assign Provider Roles

The `role` field describes what a provider does inside this node:

- `executor`: runs the node prompt and produces candidate output. This is the
  default when `role` is omitted or when the provider is written as a string.
- `reviewer`: reviews executor output inside a sequential review loop. Reviewers
  do not produce the initial candidate.

Allowed roles depend on the node mode. Input nodes do not declare providers.
Parallel nodes only allow executors. A sequential node with one provider is also
executor-only. A sequential node with executors followed by reviewers becomes a
review loop.

## Run One Provider

Use `parallel` with one provider for the common single-agent case:

```yaml
nodes:
  - id: review.project
    mode: parallel
    providers: [codex]
```

This renders one prompt, invokes one executor provider, and writes one node
result.

## Fan Out To Multiple Providers

`mode: parallel` renders the node prompt once and sends the same executor prompt
to every provider at the same time:

```yaml
nodes:
  - id: compare.designs
    mode: parallel
    providers: [codex, claude, gemini]
```

Each provider writes its own stage artifact. Finalization aggregates the latest
output for each provider task into the node result, so downstream
`{{compare.designs.output}}` contains one section per selected provider output.

Parallel mode rules:

- Providers are executors. `role: reviewer` is not valid.
- Sequential and review-loop controls are not valid.
- `failure_threshold` is valid only for parallel nodes.
- `settings.max_parallel_invocations` can cap provider calls inside the node.

Useful cases:

- Compare multiple models on the same question.
- Generate several independent proposals before a later synthesis node.
- Run redundant providers so one timeout or quota failure does not block useful
  output.
- Ask independent agents to inspect the same artifact, then feed the combined
  result to a downstream node.

Failure controls:

```yaml
nodes:
  - id: inspect
    mode: parallel
    providers: [codex, claude, gemini]
    failure_threshold: 1
    continue_on_failure: true
```

By default, no failures are allowed. `failure_threshold: 1` allows one provider
failure. If failures exceed the threshold, `continue_on_failure: true` lets the
node complete and preserves synthetic failure artifacts in the stage output.

Parallel mode is not a review loop. If you need an executor followed by one or
more reviewers, use `mode: sequential`.

## Run One Provider In Rounds

A sequential node with one provider runs one executor through ordered rounds.
Use it when later rounds should build on the previous candidate instead of
running independent providers in parallel:

```yaml
nodes:
  - id: implement
    mode: sequential
    providers: [codex]
    depth: 2
```

The provider must be an executor. `depth` is the total number of executor
rounds, so `depth: 2` runs the provider twice in sequence. The node result comes
from the last completed round.

Use this shape when the node needs ordered executor retries or when later rounds
should operate on the previous candidate workspace state. It does not add review
feedback; for review feedback, use a multi-provider sequential node.

## Executor + Reviewer Loop

A sequential node with multiple providers becomes a review loop. Use this shape
when executor output must be reviewed before the node finalizes. Providers must
be declared as executor providers followed by reviewer providers:

```yaml
nodes:
  - id: implement
    mode: sequential
    providers:
      - provider: codex
        role: executor
      - provider: claude
        role: reviewer
```

At this level, the important choice is the node shape: a multi-provider
sequential node gives reviewers a chance to approve, block, or drive
remediation. Review loops add their own controls: `audit_rounds` sets how many
fresh review passes can run, `depth` sets how many remediation attempts can run
inside each pass, and `review_starts_with` can start the loop with reviewer
context instead of an executor candidate.

## Next

Continue to [Review Loops](review-loops.md) when a node uses executor and reviewer providers.

Or return to the [Guides](../index.md#guided-tutorial-track).
