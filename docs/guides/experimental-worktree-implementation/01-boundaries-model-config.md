# Experimental Worktree Implementation: Boundaries, Workflow Model, and Config Model

Developer-facing implementation guide for the Experimental worktree implementation described by
[ADR 0016](../../architecture/adr/0016-node-scoped-git-workspace-isolation.md), which is the source
of truth for the accepted design. This guide uses the current authored
model: workflow-level `worktrees`, node-level `worktree` selectors,
`kind: worktree` for mutable lineage, `kind: snapshot` for writable disposable
non-lineage workspaces, and `worktree: none` for project-root execution.

## Architecture Boundaries
### Core
Core owns:

- Config models for `settings.workspace`.
- Removal of `settings.default_workspace` from the supported config schema.
- Workflow schema models for top-level `worktrees` declarations and node-level
  `worktree` selectors.
- Effective workspace policy normalization.
- Validation of same-logical-worktree source-line ordering and fan-in rules.
- Preflight model additions for workspace policies, workspace-file locators,
  invocation-source metadata, Git source identity, and the
  selected `worktree_contract` mode.
- Workflow signature inputs for workspace policy, Git source identity,
  workspace-file locator metadata, project-source blob records,
  execution-scoped workspace config, dependency graph, and lineage
  declarations.
- Diagnostics for invalid workflow or preflight state.

Core does not allocate worktrees, import bundles, invoke provider CLIs, mutate
Git metadata, clean cache directories, or infer provider behavior.

### Architecture Ports
Architecture ports own the replaceable integration contract.

The base `InvokerAdapterPort` remains sufficient for disabled-mode execution.
Workspace-enabled execution uses an additional optional capability protocol. The
loader and composition root normalize missing workspace capability metadata to
`supported: false`; this only fails when `settings.workspace.enabled: true` and
real workspace execution would invoke providers.

```python
@dataclass(frozen=True)
class InvokerWorkspaceSupport:
    supported: bool
    launch_mode: Literal[
        "runtime_command_runner",
        "mock_no_child_process",
    ] | None
    honors_cwd: bool
    controlled_child_environment: bool
```

```python
@dataclass(frozen=True)
class InvokerAdapterCapabilities:
    workspace: InvokerWorkspaceSupport
```

```python
class WorkspaceCompatibleInvokerAdapter(Protocol):
    def workspace_capabilities(self) -> InvokerAdapterCapabilities: ...
```

Workspace-enabled real execution accepts only these v1 modes:

- `runtime_command_runner`: provider child processes are launched through the
  runtime-owned `CommandRunner` with runtime-supplied `cwd` and
  child-environment controls. Built-in `cli` uses this mode.
- `mock_no_child_process`: no provider child process is launched; invocations
  record `cwd` and workspace context for deterministic tests. Built-in `mock`
  uses this mode.

Workspace-enabled real execution rejects:

- invokers with no workspace capability record
- invokers that do not honor `cwd`
- process-based invokers that bypass the runtime-owned command runner
- future non-process/API invokers until a separate ADR defines their workspace
  contract

Disabled-mode execution does not require adapters to implement
`workspace_capabilities()`. This keeps the feature flag from leaking into the
default project-root path while still making enabled-mode launch behavior
explicit.

The capability record is adapter metadata, not workflow policy. Runtime still
owns workspace policy, source policy, Git contract enforcement,
invocation-source selection, and workspace lifecycle.

### CLI Run Preflight
When workspace isolation is disabled, CLI preflight uses the existing
project-root behavior. It does not run Git discovery, Git capability probes,
source policy, cache-root validation, workspace locator compilation, invoker
workspace compatibility validation, or workspace resource estimation.

When workspace isolation is enabled, CLI run preflight owns the side-effect-free
source gate before duplicate lookup and run allocation:

1. Parse, validate schema, compose imports, and normalize workspace policy.
2. Reject unknown settings keys, including stale `settings.default_workspace`.
3. Validate selected invoker workspace compatibility when a real run would
   invoke providers.
4. Fail native Windows before workspace-enabled Git probing or mutation.
5. Discover Git repository context.
6. Capture `run_base_commit`, source tree, object format, top-level path,
   active Git directory, common Git directory, repository id digest, Git
   version, and required read-only probe results.
7. Verify `blob_exact` workspace contract capability support.
8. Inspect local Git config and active worktree config state.
9. Classify local/worktree Git config into rejected, overridden, and
   ignored-neutral keys.
10. Probe filesystem support for runtime-required symlink, executable-bit,
    case, Unicode, permission, and locking behavior.
11. Validate local Git attribute and ignore sources.
12. Validate the source tree has no effective byte-transforming attributes on
    tracked regular files.
13. Validate source-tree path safety, including collisions that would break the
    active filesystem's case or Unicode behavior.
14. Compile project-source workspace-file locator initial records with Git
    tree/object reads and canonical blob-byte digests using literal path
    semantics.
15. Validate configured workspace cache placement.
16. Enforce clean-start policy.
17. Detect unsupported repository states.
18. Estimate workspace storage pressure.
19. Pass a neutral `WorkspaceSourceSnapshot` into preflight compilation.

`orchestrator validate` and `orchestrator run --dry-run` may run read-only Git
probes, Git object reads, `git config --no-includes`, literal-path tree lookups,
and attribute inspection. They do not allocate run directories, worktrees,
bundles, workspace-state files, cached refs, lock files, workspace cache
children, or cleanup state.

Workspace-enabled validate and dry-run require enough Git source context to
compile and validate the enabled-mode execution plan. They fail clearly when no
Git repository or unsupported Git source state is present.

Status-style preflight commands must use `git --no-optional-locks` and
`GIT_OPTIONAL_LOCKS=0`, or an equivalent non-mutating tracked-state collector.
Preflight must not refresh or write the project index. If the active checkout
index is missing, corrupt, unreadable, contains hidden index flags that
invalidate clean-start, or relies on split-index/fsmonitor/untracked-cache state
that v1 rejects, workspace-enabled source policy fails with remediation instead
of creating, repairing, or normalizing it.

### Runtime Workspace Service
The workspace service is runtime-owned in v1, not a new pluggable adapter
surface.

In enabled mode, runtime owns:

- snapshot provisioning
- worktree provisioning
- source commit and upstream bundle resolution
- per-invocation source identity selection
- runtime resolution of workspace-file locators with Git tree/object reads and
  literal path semantics
- enforcement of the selected `worktree_contract`
- candidate and final lineage commits
- result capture from final worktree filesystem changes
- rejection of observable final `HEAD` movement and protected-state drift
- bundle export and import
- cached-ref management
- POSIX repository locking
- workspace cleanup and retention
- protected Git identity and ref verification
- runtime-owned child-process launch controls for supported process invokers

A future workspace backend port may be introduced only after more than one
backend is real.

### Artifact Store
The artifact store remains the canonical persistence boundary. Real execution
requires a filesystem-backed artifact store in v1 because same-context locking,
run-history scan, resume hydration, duplicate-skip verification, run output
allocation, and cleanup require local filesystem paths. Workspace-enabled
execution additionally depends on local paths for Git bundles and
workspace-state files.

The filesystem artifact backend writes:

- `workspace-state.json`
- `workspace.bundle`
- workspace diagnostics
- tracked-only excluded-file notes
- workspace summaries in preflight and run summaries
- rendered workspace-file digest descriptors
- invocation-source descriptors
- node-state descriptors pointing to workspace lineage artifacts

A non-filesystem artifact backend may support `validate` and `run --dry-run`,
but real execution fails before lock, skip, resume, or full-run semantics until
that backend implements an equivalent local materialization and integrity
contract.

### Invoker Boundary
Provider invocation stays provider-neutral, but workspace-enabled real
execution requires explicit invoker workspace compatibility.

`AgentInvoker.invoke(...)` requires explicit `cwd: Path`. Runtime passes the
effective invocation directory and workspace diagnostics through neutral
arguments.

For process-based invocation, the built-in CLI invoker routes child processes
through the runtime-owned `CommandRunner`. The command runner applies `cwd` and
workspace child-environment controls.

`InvocationContext` records workspace metadata for diagnostics, observability,
mock assertions, and failure reports.

`InvocationPlan` remains provider command shape only:

- argv
- stdin payload
- structured-output paths
- parser/profile selections
- failure-classification profile
- log headers

`InvocationPlan` must not carry workspace policy, `cwd`, environment policy, Git
policy, or invocation-source policy.

Provider adapters must not infer workspace behavior from provider names,
executable names, flags, output formats, quota text, usage text, sandbox mode,
permission model, or Git state.

## Workflow Model
Workflow files own their logical workspace shape through a workflow-level
`worktrees` mapping. The mapping is valid only when
`settings.workspace.enabled: true`.

When disabled, an authored workflow `worktrees` declaration or explicit node
`worktree` selector fails validation with remediation:

```text
Workspace config requires settings.workspace.enabled: true. Enable workspace isolation or remove workspace declarations.
```

Enabled-mode workflow config:

```yaml
worktrees:
  implementation_worktree:
    kind: worktree
    setup_profile: node_dependencies
    create_branch: true

  review_snapshot:
    kind: snapshot
```

Rules:

- `worktrees` is workflow-scoped and is not read from project config.
- Each mapping key is a logical worktree name for the current workflow run.
  `none` is reserved for explicit node opt-out and cannot be a worktree name.
- A `kind: worktree` entry is a Git-backed mutable source line that can emit
  `workspace-state.json`, `workspace.bundle`, and optional local branch export.
- A `kind: snapshot` entry is writable disposable scratch space. It never emits
  source lineage or a branch.
- Same logical worktree name inside one workflow run means nodes continue the
  same mutable source line.
- Different logical worktree names mean independent source lines.
- `setup_profile`, `create_branch`, and `branch_name` are valid only for
  `kind: worktree`.
- `create_branch` defaults to false.

Node selection rules:

- If a workflow declares exactly one worktree entry, provider nodes inherit it
  by default.
- If a workflow declares multiple worktree entries, every provider node that
  should run in a managed workspace must name one explicitly.
- If a workflow declares multiple worktree entries, a provider node that should
  run without a managed workspace must explicitly set `worktree: none`.
- `worktree: none` uses project-root execution, produces no source lineage, and
  does not let downstream code state inherit file mutations.
- A node naming a worktree not declared under `worktrees` fails validation.
- Input nodes never allocate provider workspaces and reject explicit
  `worktree` selectors.

Example:

```yaml
nodes:
  - id: implement
    needs: [design]
    providers: [codex]
    worktree: implementation_worktree

  - id: review
    needs: [implement]
    providers: [claude]
    worktree: review_snapshot
```

Source compilation rules:

1. A node with no managed worktree or `worktree: none` uses project-root
   execution and no workspace source lineage is compiled.
2. A `kind: snapshot` node materializes from the run's recorded project source
   and produces no downstream lineage.
3. A `kind: worktree` node with no direct upstream dependency selecting the
   same worktree name starts from the recorded project source.
4. A `kind: worktree` node with an ordered direct upstream selecting the same
   worktree name starts from that upstream's verified result commit.
5. Parallel or unordered writers selecting the same worktree name fail
   validation because one mutable source line cannot fork and merge implicitly.
6. A direct upstream selecting a different `kind: worktree` name cannot be
   merged implicitly. Cross-worktree information flows through ordinary
   artifacts, `worktree: none`, or `kind: snapshot`.

Because workspace isolation is disabled by default, non-Git project execution
remains supported by default. Git is required only when a workflow selects
managed `worktrees` and `settings.workspace.enabled: true`.

## Config Model
Remove the dormant compatibility surface:

```yaml
settings:
  default_workspace: ".orchestrator/workspaces"
```

`settings.default_workspace` is not valid in the current schema after this ADR.
Generated templates and docs remove it. Validation fails through the strict
settings schema:

```text
settings.default_workspace: Extra inputs are not permitted
```

Add opt-in workspace config:

```yaml
settings:
  workspace:
    enabled: false
    cache_root: null
    cleanup_on_success: true
    worktree_contract: "blob_exact"
    clean_start: "strict"
    setup_profiles: {}
    setup_timeout_seconds: 600
    identity:
      include_cache_root: false
    max_concurrent_materializations: 1
    disk:
      warn_free_bytes: null
      fail_free_bytes: null
```

Rules:

- Missing `settings.workspace` is equivalent to `enabled: false`.
- `enabled: false` preserves current project-root execution semantics.
- `enabled: true` is only a gate; no workspace is allocated unless the workflow
  declares `worktrees` and a provider node selects one.
- `worktree_contract: blob_exact` is the only accepted v1 contract mode.
- `clean_start` must be `strict` or `tracked_only`.
- `setup_profiles` maps profile names to audited setup command lists. Profiles
  run only for selected `kind: worktree` nodes that opt into them.
- `setup_timeout_seconds` bounds each setup command sequence.
- `identity.include_cache_root` defaults false. Cache placement is an execution
  fact by default, not semantic identity.
- `max_concurrent_materializations` serializes v1 materialization by default.
- Disk guardrails can warn or fail using byte thresholds before provider cost.
- `cache_root: null` resolves to:
  - macOS: `~/Library/Caches/orchestrator-cli`
  - Linux/POSIX/WSL: `${XDG_CACHE_HOME:-~/.cache}/orchestrator-cli`
- Native Windows is unsupported for workspace-enabled runs. Users should use
  WSL or another POSIX environment.
- A configured `cache_root` must be absolute.
- Project-root-relative cache roots are rejected.
- The unresolved spelling and canonical real path must both pass overlap checks.
- The cache root must not equal, contain, or be contained by the project root,
  project `.orchestrator/`, `.orchestrator/execution-stages/`,
  `.orchestrator/execution-results/`, `.orchestrator/locks/`, Git common
  directory, or active checkout Git directory.
- On case-insensitive filesystems, overlap checks also compare normalized
  case-folded paths.
- The final existing cache root path must not be a symlink.
- Runtime rejects pre-existing symlinks at orchestrator-created child paths.
- Cache roots and workspace directories are created owner-private where
  supported.
- On POSIX, cache roots, run cache roots, workspaces, snapshots, and lock
  directories are created with `0o700` where supported.
- Operation-scoped temporary Git index files are created under owner-private
  system temporary directories, not under the persistent workspace cache root.
- Workspace provisioning is bounded by `max_concurrent_materializations` and
  serialized for repository metadata mutation in v1.
- Git metadata mutations are serialized by repository locks.
- `cleanup_on_success: true` removes successful live workspace cache directories
  after terminal success.
- Failed and cancelled active workspace cache directories are cleaned up
  best-effort after terminal state is recorded. Dirty terminal checkouts are not
  lineage or resume sources.
- Canonical artifacts remain controlled by artifact retention.
- Runtime may write persistent cached final refs when possible. Cached refs are
  accelerators only and have no user-facing toggle in v1.

Rejected v1 knobs:

- `default_workspace`
- `max_concurrent_provisioning`
- `min_free_bytes`
- `disk_pressure_warn_fraction`
- retention tri-state keys
- `cached_refs`

Signature scope:

- `settings.workspace.enabled` affects execution behavior. Disabled and missing
  are equivalent; enabled changes workflow identity.
- `settings.workspace.cache_root` is excluded from semantic identity by default.
  When `identity.include_cache_root: true`, cache placement affects
  workspace-enabled workflow identity because provider CLIs can observe `cwd`.
- `settings.workspace.cleanup_on_success` is recorded in run metadata but does
  not affect workflow identity unless behavior changes provider invocation or
  lineage artifacts.
- Workflow-level `worktrees` declarations and node `worktree` selectors affect
  workflow identity only in enabled runs.
- Run base commit, source tree, Git object format, repository id digest,
  dependency graph, and lineage declarations affect enabled-mode workflow
  identity.
- The selected `worktree_contract` mode and schema version affect enabled-mode
  workflow identity.
- Project-source workspace-file locator metadata, Git blob identities, canonical
  blob-byte SHA-256 digests, and literal path resolution metadata affect
  workflow identity.
- Runtime invocation-source commits for reviewers/remediation and rendered bytes
  from future candidates are execution outputs and are excluded from the initial
  workflow signature.
- Run-specific workspace paths are excluded from workflow identity.
