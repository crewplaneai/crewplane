# ADR 0003: Mock Invoker Adapter

## Status
Accepted and implemented

## Date
2026-04-10

## Context
Validating full workflow execution, tmux dashboards, progress timelines, and DAG
state transitions used to require external provider CLIs. That made local checks
costly, credential-dependent, network-sensitive, and hard to reproduce.

The runtime architecture already resolves invoker implementations through the
ports-and-adapters boundary:

`InvokerAdapterPort.create_invoker() -> AgentInvoker.invoke()`

That boundary lets the project replace provider transport without changing the
workflow scheduler, artifact manager, or UI runtime.

## Decision
Implement a built-in `mock` invoker adapter as the primary cost-free,
deterministic end-to-end execution mechanism.

- Configure it with `settings.integrations.invoker.implementation: "mock"`.
- Keep orchestration core behavior unchanged.
- Add the built-in `mock` alias through the integration registry and loader.
- Support artificial delays, output shaping, deterministic fixture lookup,
  deterministic failure selectors, findings-friendly lorem output, and
  review-loop-friendly reviewer contracts.
- Defer custom script invokers, full record/replay, provider-specific CLI
  simulation, and per-provider lorem profiles.

## Goals
- Zero-cost workflow execution from day one.
- No external credentials, scripts, provider CLIs, or setup.
- Config-driven behavior for delays, output shaping, fixtures, and failures.
- Deterministic outputs for stable local verification, integration tests, and
  screenshots.
- Strict option validation that fails fast on bad config.
- Preserve the existing blackboard architecture by writing normal artifacts to
  `.crewplane/`.

## Non-Goals
- Perfect simulation of provider-specific CLI behavior.
- Capturing and replaying full historical runs.
- Emulating provider quota, retry, crash payload, or streaming-token behavior.
- Adding review-specific behavior to the runtime transport contract outside the
  invoker adapter.

## Configuration

```yaml
settings:
  integrations:
    invoker:
      implementation: "mock"
      options:
        delay_seconds: 1.5
        observation_delay_seconds: 5
        output_mode: "lorem" # "lorem" | "echo" | "file"
        output_dir: ".crewplane/mock_outputs" # required when output_mode="file"
        strict_file_mode: false
        seed: 42
        fail_when:
          - node_id: "backend.auth"
          - node_id: "summary.final"
            provider: "claude"
            role: "reviewer"
            audit_round_num: 1
            round_num: 2
```

## Option Schema
Validation happens inside `MockInvokerAdapter.create_invoker(...)`. Unknown
options raise `ValueError`, matching existing adapter behavior.

| Option | Type | Default | Rules |
|---|---|---|---|
| `delay_seconds` | `int \| float` | `0` | Must be finite and `>= 0`; booleans are rejected |
| `observation_delay_seconds` | `int \| float` | `5` | Must be finite and `>= 0`; booleans are rejected |
| `output_mode` | `str` | `"lorem"` | Normalized to lowercase; must be `lorem`, `echo`, or `file` |
| `output_dir` | `str \| null` | `null` | Required and non-empty when `output_mode=file`; resolved to an absolute `Path` |
| `strict_file_mode` | `bool` | `false` | If true and no fixture is found, fail invocation |
| `seed` | `int \| null` | `null` | Adds a deterministic seed marker to lorem output |
| `fail_when` | `list[dict]` | `[]` | Each dict is a non-empty selector using supported keys |

Supported `fail_when` selector keys:

- `node_id: str`
- `task_id: str`
- `provider: str`
- `role: str`
- `audit_round_num: int`
- `round_num: int`

A selector matches when all provided keys equal the current
`InvocationContext` fields. A matching selector raises `RuntimeError` with a
selector summary.

## Invocation Behavior
For each `invoke(...)` call:

1. Sleep for `delay_seconds`.
2. Evaluate `fail_when` against `invocation_context`; if matched, raise
   `RuntimeError`.
3. Sleep for `observation_delay_seconds` during output resolution. This keeps
   successful mock invocations visibly active in live UIs. Forced failures
   currently skip this observation delay after `delay_seconds`.
4. Resolve output text by `output_mode`.
5. Write UTF-8 output to `output_file`.
6. Apply fixture mutation sidecars when a file fixture supplied the output.
7. If `log_file` is provided, append one JSON record summarizing mode, source,
   fixture path, output path, and invocation context fields.

### `echo` Mode
For non-reviewer invocations, write the prompt exactly as received.

For reviewer invocations, write a deterministic no-findings review contract
instead of the prompt. This keeps sequential review-loop tests able to complete
without requiring each prompt fixture to contain valid reviewer verdict markup.

### `lorem` Mode
For non-reviewer invocations, write deterministic placeholder Markdown:

- `# Mock Invocation Output`
- node, task, provider, role, audit round, and local round identity
- optional seed-derived marker
- compact `Summary`, `Notes`, and `Next Steps` sections
- exactly one deterministic `<!-- findings --> ... <!-- /findings -->` block
  when `invocation_context.findings_enabled` is true

For reviewer invocations, write the same deterministic no-findings review
contract used by reviewer `echo` fallback.

### `file` Mode
Resolve a Markdown fixture from `output_dir`. If a fixture is found, write it
exactly as authored.

When `audit_round_num` is present, grouped audit-round fixtures are checked
before flat node-level fixtures:

1. `{output_dir}/{node_id}/review-audit-round-{audit_round_num}/{task_id}_round{round_num}.md`
2. `{output_dir}/{node_id}/review-audit-round-{audit_round_num}/{role}-round-{round_num}.md`
3. `{output_dir}/{node_id}/review-audit-round-{audit_round_num}/{task_id}.md`
4. `{output_dir}/{node_id}/review-audit-round-{audit_round_num}/{role}.md`
5. `{output_dir}/{node_id}/review-audit-round-{audit_round_num}/default-{role}.md`

Then flat node-level and global fixtures are checked:

1. `{output_dir}/{node_id}/{task_id}_round{round_num}.md`
2. `{output_dir}/{node_id}/{role}-round-{round_num}.md`
3. `{output_dir}/{node_id}/{task_id}.md`
4. `{output_dir}/{node_id}/{role}.md`
5. `{output_dir}/{node_id}.md`
6. `{output_dir}/default-{role}.md`
7. `{output_dir}/default.md`

If no fixture is found:

- `strict_file_mode=true`: raise `RuntimeError`.
- reviewer context: write the deterministic no-findings review contract.
- non-reviewer context: fall back to lorem output with the same
  findings-enabled behavior.

If `invocation_context` is missing entirely, file mode only checks
`{output_dir}/default.md`.

### Fixture Mutation Sidecars
A resolved fixture can include a sibling `<fixture-name>.mutations.json` file to
simulate provider-side artifact drift during integration tests.

The sidecar must be a JSON list of objects:

```json
[
  {
    "path": "review-state/mutated-note.md",
    "content": "mutation applied"
  }
]
```

Each mutation writes `content` as UTF-8 text under the invocation output
directory. Paths must remain inside that directory; path traversal is rejected
with `RuntimeError`.

### Invocation Logs
When `log_file` is provided, the mock invoker appends one JSON object per
invocation. The record includes:

- `invoker`
- `output_mode`
- `source`
- `fixture_path`
- `output_file`
- `node_id`
- `task_id`
- `provider`
- `role`
- `audit_round_num`
- `round_num`

## Acceptance Coverage

Adapter behavior tests cover:

1. Default `lorem` output.
2. `None` model handling.
3. Exact non-reviewer `echo` output.
4. Structured no-findings reviewer contracts in `echo`, `lorem`, and fallback
   `file` behavior.
5. Findings block emission for findings-enabled lorem output and file fallback.
6. `delay_seconds` elapsed-time behavior.
7. `fail_when` selectors by single, composite, and audit-round criteria.
8. Exact file fixture resolution.
9. Audit-round grouped fixture precedence.
10. Role-aware fixture fallback.
11. Strict file-mode failure.
12. Non-strict file-mode fallback.
13. Fixture mutation sidecars and path traversal rejection.
14. Deterministic seeded lorem output.
15. Unknown option rejection.
16. Invalid option types and values.
17. Non-string option and selector-key rejection.
18. Structured invocation log appends with audit-round fields.

Architecture and CLI tests cover:

1. Registry alias `mock` for `invoker`.
2. Sorted `allowed_implementations("invoker")` including `mock`.
3. Loader resolution from alias to built-in class path.
4. Loader instantiation of `MockInvokerAdapter`.
5. Container wiring that runs a workflow through the mock invoker.
6. Findings artifact writing from mock lorem output.
7. CLI executable validation being skipped for mock invoker runs.
8. Config preservation of `observation_delay_seconds`.

## Manual Verification

1. Set `settings.integrations.invoker.implementation: "mock"` in
   `.crewplane/config.yml`.
2. Run `crewplane run` with tmux UI enabled.
3. Verify state transitions:
   - node: `pending -> running -> succeeded/failed`
   - invocation: `pending -> running -> succeeded/failed`
4. Repeat with:
   - `delay_seconds > 0`
   - `observation_delay_seconds: 0` for fast test runs
   - at least one `fail_when` selector
   - `output_mode=file` with partial fixtures to observe fallback behavior
   - fixture mutation sidecars when testing artifact-drift detection

## Consequences

### Positive
- Zero-cost deterministic test runs and faster local development.
- Full workflow, artifact, findings, and review-loop paths can be exercised
  without provider CLIs.
- Failure states can be induced explicitly through `fail_when`.
- Fixture mode supports realistic scripted outputs without changing runtime
  semantics.
- UI and observability behavior can be validated with predictable timing.

### Negative
- Synthetic text does not perfectly represent real model outputs.
- Reviewer fallbacks intentionally emit no-findings contracts, which is useful
  for deterministic review-loop validation but not an exact provider mimic.
- Mock execution does not expose provider-specific CLI transport, quota,
  streaming, or crash edge cases.
- Forced failures currently skip `observation_delay_seconds` after the initial
  `delay_seconds`.

## Risks and Mitigations

- Risk: synthetic text may not resemble real model output.
  - Mitigation: use `file` mode plus `default.md` and role-specific fixtures.
- Risk: fixture naming confusion.
  - Mitigation: keep lookup order explicit in README, example templates, and
    this ADR.
- Risk: silent misconfiguration.
  - Mitigation: strict option validation and unknown-option rejection.
- Risk: mock behavior leaks into runtime orchestration semantics.
  - Mitigation: keep mock-specific behavior in
    `src/crewplane/adapters/invokers/mock.py` and its
    `src/crewplane/adapters/invokers/mock_invoker/` implementation
    package.
- Risk: review-contract helpers pull the mock adapter toward runtime consensus
  internals.
  - Mitigation: review verdict constants, sentinels, parsed result data, and
    render helpers live in `core/review_contract.py`, a core-neutral contract
    consumed by both runtime consensus and the mock invoker.

## Rejected or Deferred Alternatives

1. Script invoker adapter: deferred. Custom scripts are flexible, but they add
   setup overhead for the main UI and orchestration validation use case.
2. Full record/replay invoker: deferred. Historical run replay requires a
   robust manifest and matching strategy that is larger than the immediate mock
   validation need.
3. Provider-specific CLI simulation: out of scope for the mock adapter. Real
   provider transport remains covered by the `cli` invoker and provider-focused
   tests.
4. Per-provider lorem profiles: deferred until synthetic output style becomes a
   demonstrated limitation.

## Updates

- **2026-04-10**: Mock `lorem` output emits a deterministic findings block for
  findings-enabled invocations so findings workflows can be validated end to
  end without provider CLIs. Mock runs continue to rely on runtime fallback
  usage telemetry rather than mock-specific token simulation.
- **2026-04-20**: Mock `file` mode supports role-aware fixture fallbacks for
  sequential review-loop validation:
  - `{output_dir}/{node_id}/{task_id}_round{round_num}.md`
  - `{output_dir}/{node_id}/{role}-round-{round_num}.md`
  - `{output_dir}/{node_id}/{task_id}.md`
  - `{output_dir}/{node_id}/{role}.md`
  - `{output_dir}/{node_id}.md`
  - `{output_dir}/default-{role}.md`
  - `{output_dir}/default.md`
  This keeps review-loop fixture scripting inside the mock adapter rather than
  adding review-specific runtime transport behavior.
- **2026-04-22**: Mock `file` mode supports grouped audit-round fixtures and
  selectors:
  - `fail_when` and log records accept `audit_round_num`
  - when `audit_round_num` is present, grouped lookups under
    `{output_dir}/{node_id}/review-audit-round-{audit_round_num}/` are checked
    before flat node-level fixtures
  - grouped directories may carry their own `default-{role}.md` fallback
- **2026-05-10**: Consolidated the v1 design note into this ADR and documented
  current implementation details, including reviewer contracts, mutation
  sidecars, audit-round selectors, validation coverage, and deferred work.
- **2026-06-05**: Moved the mock helper modules into the
  `src/crewplane/adapters/invokers/mock_invoker/` package while keeping
  `crewplane.adapters.invokers.mock:MockInvokerAdapter` as the stable
  registry alias target.
- **2026-06-07**: Folded in the boundary hardening update. The mock adapter
  remains an adapter-owned deterministic transport, uses the core-neutral
  review contract, and does not add review-specific behavior to runtime provider
  invocation contracts.
