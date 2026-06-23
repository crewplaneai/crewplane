# Experimental Worktree Implementation: Retries, Review Loops, and Invocation Contract

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Invocation Attempts and Retries
Workspace mutation is scoped to logical node execution, not to provider
transport attempts.

Before each provider invocation attempt, runtime records a baseline:

- `kind: snapshot`: source commit, selected `worktree_contract`, and snapshot
  digest
- `kind: worktree`: current runtime candidate commit, current tree, expected
  `HEAD`, absence of active worktree config, selected `worktree_contract`, and
  clean workspace state

A mutable workspace should be clean at the start of every transport attempt.
Accepted executor progress is represented by runtime-owned candidate commits.
Retryable failures do not inherit partial file edits, staged state,
provider-created refs, final `HEAD` movement, or worktree-specific config.

If an attempt fails because of timeout, quota retry, cancellation, provider
crash, or another retryable transport/runtime failure:

1. Terminate the process group through the existing invocation cancellation
   path.
2. Preserve provider logs and diagnostics.
3. Reset the worktree to the attempt baseline using Git reset/clean operations
   under runtime control.
4. Remove tracked, untracked, ignored generated changes, staged state,
   provider-created refs in the run namespace, and provider-created worktree
   config produced by the failed attempt.
5. Reject if provider `HEAD` movement or Git metadata mutation cannot be safely
   reset to the baseline.
6. Verify the workspace is clean and `blob_exact` still passes.
7. Retry if retry policy allows.

V1 does not preserve ignored caches across retry attempts. Deterministic retry
state is more important than build-cache reuse.

A final failed or cancelled node records diagnostics and terminal workspace
state under `.crewplane/`. Its mutated checkout is cleaned up best-effort and
is not a source of truth for later runs.

## Review Loops and Multi-Provider Nodes
Sequential review-loop remediation is different from transport retry.

Each accepted executor round starts from the current canonical candidate
workspace for that node. Runtime creates a Crewplane-owned candidate commit
after each successful executor round whose output is eligible for review.

Reviewer invocations inspect the current candidate but must not share the live
mutable executor worktree.

Reviewer rules:

- Each reviewer receives a disposable non-lineage workspace rooted at the
  current source or candidate commit.
- For mutable nodes, reviewer workspaces are rooted at the current candidate
  commit.
- Reviewer prompt file tokens resolve from the same current candidate commit
  used to provision the reviewer workspace.
- No reviewer workspace is provisioned from mutable executor output until result
  capture and candidate-tree validation have passed and a Crewplane-owned
  candidate commit exists.
- The default reviewer view is a disposable Git worktree, so reviewer CLIs may
  run tools that write temporary files.
- Reviewer workspaces use the selected `worktree_contract`.
- Reviewer workspaces are never exported as lineage.
- Reviewer source changes, final `HEAD` movement, and worktree-specific config
  are discarded with drift diagnostics.
- Parallel reviewers get distinct disposable views.
- Reviewer paths, invocation source identity, and lifecycle are recorded for
  diagnostics.
- Runtime records a reviewer baseline before invocation and checks source-tree
  drift afterward.
- Reviewer mutation of executor candidate workspace, current node lineage refs,
  protected crewplane refs, local Git config/attribute/ignore sources,
  worktree config, or canonical artifacts fails the node when attribution is
  safe.
- Concurrent shared-artifact mutation follows existing destructive-drift
  attribution rules.

Remediation executor rules:

- Remediation rounds start from the current canonical candidate workspace.
- Remediation prompt file tokens resolve from the current candidate commit, not
  from the original node source commit.
- If remediation capture succeeds, runtime creates the next candidate commit and
  subsequent reviewers see that new candidate source.

For v1, a mutable `kind: worktree` node supports one executor provider. A future
multi-executor mutable design must define explicit candidate selection, merge,
or promotion semantics before downstream lineage can be produced.

## Invocation Contract
The invocation contract changes to make `cwd` explicit and to make
workspace-compatible process launch enforceable at the adapter boundary.

```python
class AgentInvoker(Protocol):
    async def invoke(
        self,
        config: AgentConfig,
        model: str | None,
        prompt: str,
        output_file: Path,
        *,
        cwd: Path,
        log_file: Path | None = None,
        invocation_context: InvocationContext | None = None,
    ) -> None: ...
```

A child-process launch helper owned by runtime computes environment changes
from `cwd` and `InvocationContext.workspace`. It is used by the built-in CLI
invoker when calling the command runner. Provider adapters do not compute
workspace environment policy.

```python
@dataclass(frozen=True)
class ChildProcessEnvironment:
    set: Mapping[str, str]
    unset: tuple[str, ...]
```

```python
class CommandRunner(Protocol):
    async def __call__(
        self,
        cmd: list[str],
        stdin_data: bytes | None,
        log_file: Path | None,
        append_log: bool,
        log_header: bytes | None,
        invocation_context: InvocationContext | None,
        idle_timeout_seconds: float | None,
        *,
        cwd: Path,
        child_environment: ChildProcessEnvironment | None = None,
    ) -> CommandResult: ...
```

A `None` `child_environment` means inherit the current process environment
except for existing runner behavior. A non-`None` value means unset the exact
listed variables and set the listed variables for the child process.

Workspace-enabled process invocations must use a non-`None`
`ChildProcessEnvironment`. The command runner must apply it to
`asyncio.create_subprocess_exec`. A process-based invoker that cannot guarantee
this path is not workspace-compatible in v1.

The runtime helper expands dynamic Git config-injection variables from the
current process environment into exact names before invoking the runner. This
includes `GIT_CONFIG_KEY_*` and `GIT_CONFIG_VALUE_*`. Expansion is recomputed
for each process launch and is not persisted in `workspace-state*.json`.

Runtime may use `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*` that
it creates itself to apply deterministic provider child-process Git config
overlays. These runtime-created variables are distinct from inherited caller
variables, which are unset.

`InvocationSourceContext`:

```python
@dataclass(frozen=True)
class InvocationSourceContext:
    source_kind: Literal["project", "node", "candidate"]
    source_node_id: str | None
    source_commit: str
    source_tree: str
    candidate_sequence: int | None
```

`InvocationWorkspaceContext`:

```python
@dataclass(frozen=True)
class InvocationWorkspaceContext:
    kind: Literal["snapshot", "worktree"]
    logical_worktree_name: str
    cwd: Path
    invocation_source: InvocationSourceContext
    worktree_contract: Literal["blob_exact"]
    candidate_commit: str | None
    result_commit: str | None
    disposable: bool
    lineage_producer: bool
    workspace_state_path: Path | None
    child_environment_required: bool
    child_environment_applied: bool | None
```

`InvocationContext` gains:

```python
workspace: InvocationWorkspaceContext | None = None
```

`InvocationPlan` does not gain `cwd`, environment policy, Git policy,
invocation-source policy, or workspace policy.

The built-in CLI command runner passes `cwd` and merged child environment to
`asyncio.create_subprocess_exec`.

The mock invoker records `cwd`, workspace metadata, invocation-source metadata,
child-environment status, and selected invoker workspace capability mode. It
does not need to emulate process environment mutation.

Future non-process invokers are intentionally unsupported in v1. A future ADR
may define a separate non-process workspace contract.
