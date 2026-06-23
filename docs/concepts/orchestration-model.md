# Orchestration Model

Crewplane is a local control plane for AI coding CLIs.

The core model is blackboard orchestration: providers do not coordinate through
shared in-memory state. Each provider invocation receives a rendered prompt and
writes output that Crewplane persists as artifacts under `.crewplane/`.
Downstream nodes read upstream artifacts through explicit workflow references.

## DAG Execution

Workflow nodes form a directed acyclic graph. Nodes with no unmet dependencies
can run in the same execution wave. Nodes with `needs` wait for their upstream
nodes to finish.

Supported node modes:

- `parallel`: run one or more executor providers for the same prompt.
- `sequential`: run one executor or an executor/reviewer sequence.
- `input`: load a file as a workflow input without invoking a provider.

## CLI-First Providers

Crewplane invokes provider tools as external commands configured in
`.crewplane/config.yml`. It does not use vendor SDKs for provider execution.

Provider-specific command building, retries, quota detection, prompt transport,
output parsing, and usage parsing live behind the invoker adapter boundary.

## Auditable Artifacts

Runs write readable files under:

- `.crewplane/execution-stages/`
- `.crewplane/execution-results/`

The stage tree contains logs, manifests, preflight bundles, per-node working
artifacts, and runtime state. The results tree contains consolidated node
outputs and findings.

See the [artifact reference](../reference/artifacts.md) for the concrete layout.
