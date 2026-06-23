# Mock Validation

Use the `mock` invoker to exercise workflows without provider CLI calls,
network latency, or provider spend.

## Config

```yaml
settings:
  integrations:
    invoker:
      implementation: "mock"
      options:
        delay_seconds: 0
        observation_delay_seconds: 0
        output_mode: "lorem"
        seed: 42
```

`orchestrator validate`, `orchestrator run --dry-run`, and `orchestrator run`
can all use the mock invoker. Real mock runs still write normal artifacts.

## Output Modes

- `lorem`: deterministic generated output. For non-reviewer invocations with
  `findings: true`, it also emits deterministic findings content.
- `echo`: echo the rendered prompt for non-reviewer invocations.
- `file`: read deterministic fixture files from `output_dir`.

When `output_mode: "file"` is used, `output_dir` is required.

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
