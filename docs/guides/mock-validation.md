# Mock Validation

Use the `mock` invoker to exercise workflows without provider CLI calls,
network latency, provider accounts, API keys, or provider spend. New
`crewplane init` projects use mock execution by default.

## Config

```yaml
settings:
  integrations:
    invoker:
      implementation: "mock"
      options:
        output_mode: "lorem"
        seed: 42
        delay_seconds: 0.25
        observation_delay_seconds: 5
```

`crewplane validate`, `crewplane run --dry-run`, and `crewplane run`
can all use the mock invoker. Real mock runs still write normal artifacts.
Mock output is deterministic scaffolding for orchestration checks; it is not
model output.

When mock is active, `crewplane run` prints:

```text
Mock invoker active: no provider CLI commands will be started.
```

## Output Modes

- `lorem`: deterministic generated output. For non-reviewer invocations with
  `findings: true`, it also emits deterministic findings content.
- `echo`: echo the rendered prompt for non-reviewer invocations.
- `file`: read deterministic fixture files from `output_dir`.

When `output_mode: "file"` is used, `output_dir` is required.

## File Fixtures

File mode searches fixture paths from most specific to least specific. For a
review-loop invocation, common candidates are:

```text
<output_dir>/<node_id>/review-audit-round-<audit_round>/<task_id>_round<round>.md
<output_dir>/<node_id>/review-audit-round-<audit_round>/<role>-round-<round>.md
<output_dir>/<node_id>/review-audit-round-<audit_round>/<task_id>.md
<output_dir>/<node_id>/review-audit-round-<audit_round>/<role>.md
<output_dir>/<node_id>/review-audit-round-<audit_round>/default-<role>.md
<output_dir>/<node_id>/<task_id>_round<round>.md
<output_dir>/<node_id>/<role>-round-<round>.md
<output_dir>/<node_id>/<task_id>.md
<output_dir>/<node_id>/<role>.md
<output_dir>/<node_id>.md
<output_dir>/default-<role>.md
<output_dir>/default.md
```

A fixture can have a sibling `.mutations.json` sidecar. `mutations` write under
the invocation output directory. `workspace_mutations` write under the
invocation working directory after workspace validation.

```json
{
  "required_prompt_contains": ["Implement the change"],
  "workspace_mutations": [
    {"path": "src/example.py", "content": "print('mocked')\n"}
  ]
}
```

## Strict File Mode

`strict_file_mode: true` makes missing fixtures fail instead of falling back to
generated mock output.

## Failure Selectors

Use `fail_when` to simulate failures:

```yaml
settings:
  integrations:
    invoker:
      implementation: "mock"
      options:
        fail_when:
          - node_id: implement
            provider: codex
```

Supported selector keys are `node_id`, `task_id`, `provider`, `role`,
`audit_round_num`, and `round_num`.
